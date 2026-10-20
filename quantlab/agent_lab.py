"""Experimental A/B/C evaluation of constrained local-LLM alpha proposals.

No code execution from model outputs, no broker credentials, no order endpoints.
Each expanding fold makes decisions on training/validation and observes test once.
This is NOT the Microsoft RD-Agent or TradingAgents implementation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace, asdict
import hashlib
import ipaddress
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit

import numpy as np
import pandas as pd
import requests

from .engine import Candidate, signals, simulate
from .features import build_features, FEATURE_COLUMNS
from .settings import Settings
from .diagnostics import (
    daily_net_returns, trailing_regimes, regime_attribution,
    screen_redundant_factors, diagnostic_summary, paired_block_bootstrap,
)


class AgentError(ValueError):
    pass


@dataclass(frozen=True)
class FactorProposal:
    name: str
    feature: str
    direction: str
    threshold: float

    @classmethod
    def parse(cls, record: Any) -> "FactorProposal":
        if not isinstance(record, dict) or set(record) != {"name", "feature", "direction", "threshold"}:
            raise AgentError("Factor must have exactly name, feature, direction, threshold")
        name, feature, direction, threshold = (record[x] for x in ("name", "feature", "direction", "threshold"))
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name) is None:
            raise AgentError("Unsafe/invalid factor name")
        if feature not in FEATURE_COLUMNS or direction not in ("gt", "lt"):
            raise AgentError("Non-allowlisted factor feature/operator")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or abs(threshold) > 0.25:
            raise AgentError("Invalid factor threshold")
        return cls(name, feature, direction, float(threshold))

    def compute(self, feature_frame: pd.DataFrame) -> np.ndarray:
        x = feature_frame[self.feature]
        decision = (x > self.threshold) if self.direction == "gt" else (x < self.threshold)
        return (decision & x.notna()).to_numpy(dtype=float)


class LocalLLMAgent:
    """OpenAI-compatible LOCAL endpoint. Never sends market data to the internet."""
    def __init__(self, base_url: str, model: str, *, timeout: int = 60):
        p = urlsplit(base_url)
        try:
            local = p.hostname == "localhost" or ipaddress.ip_address(p.hostname or "").is_loopback
        except ValueError:
            local = p.hostname == "localhost"
        if p.scheme != "http" or not local or p.username or p.password or p.query or p.fragment or p.path.rstrip("/") not in ("", "/v1"):
            raise AgentError("Only loopback HTTP /v1 model endpoints are permitted")
        if not model or len(model) > 128:
            raise AgentError("Invalid local model name")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.timeout = timeout

    def _ask(self, role: str, payload: dict) -> dict:
        # Ollama's OpenAI-compatible endpoint supports strict JSON-schema
        # constrained decoding. All fields are validated again after decoding.
        if role == "alpha_proposer":
            fields = {
                "type": "object", "additionalProperties": False,
                "required": ["factors"],
                "properties": {"factors": {
                    "type": "array", "minItems": 1, "maxItems": 5,
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["name", "feature", "direction", "threshold"],
                        "properties": {
                            "name": {"type": "string"},
                            "feature": {"type": "string", "enum": list(FEATURE_COLUMNS)},
                            "direction": {"type": "string", "enum": ["gt", "lt"]},
                            "threshold": {"type": "number"},
                        },
                    },
                }},
            }
        elif role == "risk_reviewer":
            fields = {
                "type": "object", "additionalProperties": False,
                "required": ["exposure_scale", "reason"],
                "properties": {
                    "exposure_scale": {"type": "number", "enum": [0, 0.5, 1]},
                    "reason": {"type": "string"},
                },
            }
        else:
            raise AgentError("Unsupported Agent role")
        req = {"model": self.model, "temperature": 0, "messages": [
            {"role": "system", "content": "You are a constrained finance RESEARCH agent. Output only one JSON object. No code or URLs. No assurances of profit. Follow the provided schema."},
            {"role": "user", "content": json.dumps({"role": role, **payload}, ensure_ascii=False, allow_nan=False)},
        ]}
        req["response_format"] = {"type": "json_schema", "json_schema": {"name": role, "strict": True, "schema": fields}}
        try:
            # Disable environment proxies and all redirects: a local endpoint must not
            # redirect the training summary to a non-local server.
            with requests.Session() as session:
                session.trust_env = False
                res = session.post(self.url, json=req, timeout=self.timeout, allow_redirects=False)
                res.raise_for_status()
                if 300 <= res.status_code < 400:
                    raise AgentError("Model endpoint redirects are forbidden")
            content = res.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise AgentError("Model output must be a JSON string")
            body = content.strip()
            # Some local Ollama models wrap otherwise-valid JSON in one fenced
            # Markdown block. Unwrap only a WHOLE response, never extract a
            # JSON-looking substring from prose or evaluate any model code.
            import re as _re
            fenced = _re.fullmatch(r"```(?:json)?[ \t]*\n([\s\S]*?)\n```", body, flags=_re.IGNORECASE)
            if fenced:
                body = fenced.group(1).strip()
            obj = json.loads(body)
        except AgentError:
            raise
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            raise AgentError("Local model request/JSON failed; no substitute agent used") from exc
        if not isinstance(obj, dict):
            raise AgentError("Model must return JSON object")
        return obj

    def propose(self, train_summary: dict) -> list[FactorProposal]:
        response = self._ask("alpha_proposer", {"training_only": train_summary,
            "rules": "Return {\"factors\":[{\"name\":\"...\",\"feature\":\"r5\",\"direction\":\"gt\",\"threshold\":0.0}]}. At most 5 distinct factors. Allowed feature names: " + ", ".join(FEATURE_COLUMNS) + ". direction gt or lt. threshold number within [-0.25, 0.25]. No executable expressions."})
        # Exaone may put the same explicitly structured proposal one level down.
        # Never accept an arbitrary wrapper chain or model-authored expressions.
        if set(response) == {"proposal"} and isinstance(response["proposal"], dict):
            response = response["proposal"]
        if set(response) != {"factors"}:
            raise AgentError("Unexpected alpha proposal schema")
        factors = response["factors"]
        if not isinstance(factors, list) or not 1 <= len(factors) <= 5:
            raise AgentError("Must return 1-5 proposed factors")
        # Model-written names are display labels only and may be verbose,
        # Unicode or otherwise malformed. Ignore them entirely: assign
        # canonical, bounded identifiers while retaining strict validation
        # for every actual tradable feature, comparison and threshold.
        parsed = []
        for i, item in enumerate(factors, start=1):
            if not isinstance(item, dict):
                raise AgentError("Invalid factor item")
            parsed.append(FactorProposal.parse({**item, "name": f"factor_{i}"}))
        if len({(v.feature, v.direction, v.threshold) for v in parsed}) != len(parsed) or len({v.name for v in parsed}) != len(parsed):
            raise AgentError("Duplicate factors")
        return parsed

    def review(self, summary: dict) -> float:
        response = self._ask("risk_reviewer", {"validation_only": summary,
            "rules": "Return {\"exposure_scale\":0 or 0.5 or 1,\"reason\":\"short text\"}. No test data is provided. 0 means HOLD/cash."})
        # Exaone may return a single validation_results wrapper. Read only
        # the explicitly allowlisted risk_assessment values; ignore any
        # narrative or recommendation fields, which are not executable.
        if set(response) == {"validation_results"}:
            report = response["validation_results"]
            if not isinstance(report, dict) or not isinstance(report.get("risk_assessment"), dict):
                raise AgentError("Unexpected risk review schema")
            response = report["risk_assessment"]
        if set(response) != {"exposure_scale", "reason"} or type(response["exposure_scale"]) not in (int, float) or response["exposure_scale"] not in (0, 0.5, 1) or not isinstance(response["reason"], str) or len(response["reason"]) > 300:
            raise AgentError("Invalid critic schema")
        return float(response["exposure_scale"])


class MockAgent:
    """Deterministic wiring test; NOT an AI system or a profit benchmark."""
    def propose(self, train_summary: dict) -> list[FactorProposal]:
        return [FactorProposal("r5_positive", "r5", "gt", 0.0),
                FactorProposal("ma10_positive", "ma10", "gt", 0.0),
                FactorProposal("r3_negative", "r3", "lt", -0.01)]

    def review(self, summary: dict) -> float:
        # C is a deterministic, transparent risk-rule stand-in ONLY.
        r = summary["validation"]
        return 0.5 if r["sharpe"] <= 0 or r["max_drawdown"] < -0.10 else 1.0


def walk_forward_windows(n: int) -> list[tuple[int, int, int]]:
    if n < 500:
        raise ValueError("A/B/C requires >=500 full daily bars")
    # Expanding training, contiguous validation/test. Ex-post windows never overlap.
    windows = []
    for decile in (5, 6, 7, 8):
        train = n * decile // 10
        val = n * (decile + 1) // 10
        test = min(n * (decile + 2) // 10, n - 2)
        if val - train < 40 or test - val < 38:
            raise ValueError("Insufficient validation/test bars")
        windows.append((train, val, test))
    return windows


def _summarize_training(df: pd.DataFrame, until: int) -> dict:
    # Summaries are deliberately based on training days ONLY, never holdout data.
    close = df["close"].iloc[:until]
    rets = close.pct_change().dropna()
    return {"bar_count": until,
            "training_return": round(float(close.iloc[-1] / close.iloc[0] - 1), 6),
            "training_volatility": round(float(rets.std(ddof=1)), 6),
            "available_features": list(FEATURE_COLUMNS)}


def _baseline_pool(df: pd.DataFrame, train: int, *, with_lightgbm: bool = False) -> list[tuple[str, np.ndarray]]:
    pool = [("MA10-40", signals(df, Candidate("momentum", 10, 40), train)),
            ("MA20-80", signals(df, Candidate("momentum", 20, 80), train)),
            ("HGB-p0.55", signals(df, Candidate("hgb", threshold=0.55), train))]
    if with_lightgbm:
        pool.append(("LIGHTGBM-p0.55", signals(df, Candidate("lgbm", threshold=0.55), train)))
    return pool


def _best_on_validation(pool: list[tuple[str, np.ndarray]], df: pd.DataFrame, train: int, val: int,
                        settings: Settings, annual_periods: int) -> tuple[str, np.ndarray, dict]:
    evaluated = [(name, signal, simulate(df, signal, train, val, settings, annual_periods=annual_periods))
                 for name, signal in pool]
    # Rank exclusively using validation. If no trades, cash beats fake certainty.
    eligible = [item for item in evaluated if item[2]["orders"] >= 2]
    if not eligible:
        return ("CASH", np.zeros(len(df)), simulate(df, np.zeros(len(df)), train, val, settings, annual_periods=annual_periods))
    return max(eligible, key=lambda item: (item[2]["sharpe"] - 2 * abs(item[2]["max_drawdown"]), item[2]["total_return"]))


def _test_signal(df: pd.DataFrame, name: str, train: int, val: int,
                 proposed: list[FactorProposal]) -> np.ndarray:
    # Retrain ML only with history observable before this test begins.
    if name == "HGB-p0.55":
        return signals(df, Candidate("hgb", threshold=0.55), val)
    if name == "LIGHTGBM-p0.55":
        return signals(df, Candidate("lgbm", threshold=0.55), val)
    if name == "MA10-40":
        return signals(df, Candidate("momentum", 10, 40), val)
    if name == "MA20-80":
        return signals(df, Candidate("momentum", 20, 80), val)
    for proposal in proposed:
        if proposal.name == name:
            return proposal.compute(build_features(df))
    if name == "CASH":
        return np.zeros(len(df))
    raise AgentError("Unrecognized selected strategy")


def _simulate_scale(df, sig, start, end, settings, periods, scale: float):
    if scale == 0:
        sig = np.zeros(len(df))
        scale = 1
    return simulate(df, sig, start, end, replace(settings, max_position_fraction=settings.max_position_fraction * scale), annual_periods=periods)


def compare_agents(df: pd.DataFrame, settings: Settings, agent: LocalLLMAgent | MockAgent,
                   *, annual_periods: int = 252, with_lightgbm: bool = False) -> dict[str, Any]:
    if not df.index.equals(pd.RangeIndex(len(df))):
        df = df.reset_index(drop=True)
    summary = {"experiment": "A_baseline_B_agent_proposal_C_agent_risk_overlay",
               "agent_mode": "mock_not_real_ai" if isinstance(agent, MockAgent) else "local_llm",
               "data_warning": "Synthetic samples are not real investment performance",
               "promotion_eligible": False, "real_orders_enabled": False, "folds": [],
               "baseline_models": ["MA10-40", "MA20-80", "HGB-p0.55"] + (["LIGHTGBM-p0.55"] if with_lightgbm else []),
               "agent_protocol": "allowlisted_factor_proposer_plus_validation_only_risk_reviewer_not_rd_agent_or_tradingagents"}
    diagnostic_folds = []
    regime_tags = trailing_regimes(df)
    for idx, (train, val, test) in enumerate(walk_forward_windows(len(df)), start=1):
        # Agent sees only training summary, not future market candles.
        raw_proposals = agent.propose(_summarize_training(df, train))
        factors = build_features(df)
        proposals, screened = screen_redundant_factors(raw_proposals, factors, train)
        pool_a = _baseline_pool(df, train, with_lightgbm=with_lightgbm)
        pool_b = [(p.name, p.compute(factors)) for p in proposals]
        a_name, _, a_val = _best_on_validation(pool_a, df, train, val, settings, annual_periods)
        b_name, _, b_val = _best_on_validation(pool_b, df, train, val, settings, annual_periods)
        # Reviewer has NO TEST stats. B and C share identical base B signals.
        scale = agent.review({"strategy": b_name, "validation": b_val,
                              "training_bars": train, "max_position_fraction": settings.max_position_fraction})
        if scale not in (0, 0.5, 1):
            raise AgentError("Invalid risk overlay")
        a_test_sig = _test_signal(df, a_name, train, val, [])
        b_test_sig = _test_signal(df, b_name, train, val, proposals)
        results = {
            "A_baseline": {"strategy": a_name, "validation": a_val,
                "test": _simulate_scale(df, a_test_sig, val, test, settings, annual_periods, 1)},
            "B_agent": {"strategy": b_name, "validation": b_val,
                "test": _simulate_scale(df, b_test_sig, val, test, settings, annual_periods, 1)},
            "C_agent_review": {"strategy": b_name, "exposure_scale": scale,
                "test": _simulate_scale(df, b_test_sig, val, test, settings, annual_periods, scale)},
            "buy_hold_same_exposure": {"strategy": "BUY_HOLD_20PCT_CAPITAL",
                "test": _simulate_scale(df, np.ones(len(df)), val, test, settings, annual_periods, 1)},
        }
        stress_settings = replace(settings, fee_bps=settings.fee_bps * 2,
                                  slippage_bps=settings.slippage_bps * 2)
        for label, result in results.items():
            label_sig = (a_test_sig if label == "A_baseline" else
                         np.ones(len(df)) if label == "buy_hold_same_exposure" else b_test_sig)
            result["test_stress_2x_cost"] = _simulate_scale(
                df, label_sig, val, test, stress_settings, annual_periods,
                float(result.get("exposure_scale", 1)))
        # Diagnostics use only post-selection test signals and causal trailing
        # market labels. OOS evidence is NEVER returned to either LLM agent.
        paths = {}
        regime_breakdown = {}
        for label in results:
            raw_signal = (a_test_sig if label == "A_baseline" else
                          np.ones(len(df)) if label == "buy_hold_same_exposure"
                          else b_test_sig)
            used_scale = float(results[label].get("exposure_scale", 1))
            path = daily_net_returns(
                df, raw_signal, val, test, settings, scale=used_scale,
            )
            expected = float(results[label]["test"]["total_return"])
            if not np.isclose(float(np.prod(1 + path) - 1), expected, rtol=1e-9, atol=1e-10):
                raise AgentError("Diagnostic PnL must match the existing backtest")
            paths[label] = path
            regime_breakdown[label] = regime_attribution(
                df, path, val, test, regimes=regime_tags
            )
        risk_matched_path = daily_net_returns(
            df, np.ones(len(df)), val, test, settings, scale=scale
        )
        paths["buy_hold_riskmatched_C"] = risk_matched_path
        regime_breakdown["buy_hold_riskmatched_C"] = regime_attribution(
            df, risk_matched_path, val, test, regimes=regime_tags
        )
        diagnostic_folds.append(paths)
        summary["folds"].append({"fold": idx, "dates": {"train_end_exclusive": str(df.iloc[train]["date"].date()),
                               "validation_end_exclusive": str(df.iloc[val]["date"].date()),
                               "test_end_exclusive": str(df.iloc[min(test, len(df)-1)]["date"].date())},
                                 "proposals": [asdict(p) for p in proposals],
                                 "factor_screening": screened,
                                 "regime_attribution": regime_breakdown,
                                 "risk_matched_C_buy_hold": {
                                     "matched_scale": float(scale),
                                     "total_return": float(np.prod(1 + risk_matched_path) - 1),
                                 },
                                 "results": results})
    # Post-hoc analysis only; NEVER promote winning group on test results.
    for label in ("A_baseline", "B_agent", "C_agent_review", "buy_hold_same_exposure"):
        rows = [fold["results"][label]["test"] for fold in summary["folds"]]
        summary.setdefault("out_of_sample_summary", {})[label] = {
            "compounded_return_across_four_nonoverlapping_test_windows": float(np.prod([1 + r["total_return"] for r in rows]) - 1),
            "mean_fold_sharpe": float(np.mean([r["sharpe"] for r in rows])),
            "worst_fold_max_drawdown": float(min(r["max_drawdown"] for r in rows)),
            "total_orders": int(sum(r["orders"] for r in rows)),
            "compounded_return_2x_cost": float(np.prod([
                1 + f["results"][label]["test_stress_2x_cost"]["total_return"]
                for f in summary["folds"]]) - 1),
        }
    summary["diagnostics"] = diagnostic_summary(diagnostic_folds)
    summary["diagnostics"]["C_vs_riskmatched_buy_hold"] = paired_block_bootstrap(
        [f["C_agent_review"] for f in diagnostic_folds],
        [f["buy_hold_riskmatched_C"] for f in diagnostic_folds],
    )
    summary["diagnostics"]["warnings"] = [
        "All OOS comparisons are exploratory and uncorrected for strategy search.",
        "C reduces exposure; inspect the risk-matched buy-hold comparison.",
        "A positive bootstrap fraction is NOT an investment win probability.",
    ]
    summary["dataset_fingerprint"] = hashlib.sha256(pd.util.hash_pandas_object(df, index=True).values.tobytes()).hexdigest()[:16]
    return summary
