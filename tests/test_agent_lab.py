"""Offline A/B/C security, chronology, model interface, and sanity tests."""
from __future__ import annotations

import json
import numpy as np
import pytest

from quantlab.agent_lab import (
    AgentError, FactorProposal, LocalLLMAgent, MockAgent,
    compare_agents, walk_forward_windows,
)
from quantlab.data import demo_daily
from quantlab.features import build_features
from quantlab.settings import Settings


def test_rejects_untrusted_factor_code_and_nonfinite():
    valid = {"name": "momentum", "feature": "r5", "direction": "gt", "threshold": 0.01}
    assert FactorProposal.parse(valid).threshold == 0.01
    for bad in (
        dict(valid, name="__import__('os').system('echo nope')"),
        dict(valid, feature="__class__"),
        dict(valid, threshold=float("nan")),
        dict(valid, threshold=0.26),
        dict(valid, threshold=True),
        dict(valid, direction="eval"),
        dict(valid, code="print(1)"),
    ):
        with pytest.raises(AgentError):
            FactorProposal.parse(bad)


def test_rejects_external_llm_endpoints_and_credentials():
    for url in ("https://api.example.com/v1", "http://example.com/v1",
                "http://127.0.0.1.evil.com/v1", "http://127.0.0.1:8000/foo",
                "http://user:pass@127.0.0.1/v1", "http://127.0.0.1/v1?leak=true"):
        with pytest.raises(AgentError):
            LocalLLMAgent(url, "local")
    assert LocalLLMAgent("http://127.0.0.1:8000/v1", "local").model == "local"


def test_model_json_schema_rejected_without_mock_fallback(monkeypatch):
    agent = LocalLLMAgent("http://127.0.0.1:8000/v1", "test")
    monkeypatch.setattr(agent, "_ask", lambda role, payload: {"factors": [{
        "name": "x", "feature": "r5", "direction": "gt", "threshold": 0,
        "extra": "import os"}]})
    with pytest.raises(AgentError):
        agent.propose({"bar_count": 100})
    monkeypatch.setattr(agent, "_ask", lambda role, payload: {"exposure_scale": 1.5, "reason": "none"})
    with pytest.raises(AgentError):
        agent.review({"validation": {"sharpe": 0.1}})


def test_local_fenced_json_is_accepted_but_surrounding_prose_rejected(monkeypatch):
    from quantlab.agent_lab import LocalLLMAgent, AgentError

    responses = [
        '```json\n{"factors":[{"name":"m","feature":"r5","direction":"gt","threshold":0.0}]}\n```',
        'the answer is {"factors":[]}',
    ]

    class Response:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": responses.pop(0)}}]}

    class FakeSession:
        trust_env = True
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def post(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            return Response()

    monkeypatch.setattr("requests.Session", FakeSession)
    agent = LocalLLMAgent("http://127.0.0.1:11434/v1", "exaone3.5:latest")
    assert agent.propose({"bar_count": 400})[0].name == "m"
    with pytest.raises(AgentError):
        agent.propose({"bar_count": 400})


def test_model_payload_contains_training_not_future_prices(monkeypatch):
    agent = LocalLLMAgent("http://127.0.0.1:8000/v1", "test")
    requests_seen = []
    def fake_ask(role, payload):
        requests_seen.append((role, payload))
        if role == "alpha_proposer":
            return {"factors": [{"name": "x", "feature": "ma10", "direction": "gt", "threshold": 0}]}
        return {"exposure_scale": 0.5, "reason": "conservative"}
    monkeypatch.setattr(agent, "_ask", fake_ask)
    result = compare_agents(demo_daily(520), Settings(), agent, annual_periods=365)
    assert result["agent_mode"] == "local_llm"
    assert len(requests_seen) == 8
    for role, payload in requests_seen:
        body = json.dumps(payload)
        assert '"test"' not in body
        assert "test_return" not in body
        assert "future_return" not in body
    assert all(f["results"]["C_agent_review"]["exposure_scale"] == 0.5 for f in result["folds"])


def test_exaone_single_nested_wrapper_accepts_only_allowlisted_fields(monkeypatch):
    agent = LocalLLMAgent("http://127.0.0.1:11434/v1", "exaone3.5:latest")
    def nested(role, payload):
        if role == "alpha_proposer":
            return {"proposal": {"factors": [{"name": "trend", "feature": "r5", "direction": "gt", "threshold": 0.01}]}}
        return {"validation_results": {
            "strategy": "trend",
            "risk_assessment": {"exposure_scale": 0.5, "reason": "moderate risk"},
            "recommendation": "IGNORE EXTRAS",
        }}
    monkeypatch.setattr(agent, "_ask", nested)
    assert agent.propose({"bar_count": 500})[0].name == "trend"
    assert agent.review({"validation": {"sharpe": 0.5}}) == 0.5
    monkeypatch.setattr(agent, "_ask", lambda role, payload: {"proposal": {"factors": [], "code": "import os"}})
    with pytest.raises(AgentError):
        agent.propose({"bar_count": 500})


def test_walk_forward_is_chronological_with_disjoint_test_periods():
    folds = walk_forward_windows(1000)
    assert len(folds) == 4
    assert all(t < v < e for t, v, e in folds)
    assert all(folds[i][2] <= folds[i + 1][1] for i in range(3))
    # Each next validation interval starts exactly after the previous test.
    assert all(folds[i][2] == folds[i + 1][1] for i in range(3))
    with pytest.raises(ValueError):
        walk_forward_windows(499)


def test_causal_factor_values_unaffected_by_future_prices():
    old = demo_daily(520)
    proposal = FactorProposal("x", "r5", "gt", 0)
    base = proposal.compute(build_features(old))
    modified = old.copy()
    modified.loc[400:, "close"] *= 1.5
    altered = proposal.compute(build_features(modified))
    np.testing.assert_array_equal(base[:400], altered[:400])


def test_mock_comparison_never_promotes_real_trading():
    report = compare_agents(demo_daily(520), Settings(), MockAgent(), annual_periods=365)
    assert report["agent_mode"] == "mock_not_real_ai"
    assert report["promotion_eligible"] is False
    assert report["real_orders_enabled"] is False
    assert len(report["folds"]) == 4
    assert set(report["out_of_sample_summary"]) == {
        "A_baseline", "B_agent", "C_agent_review", "buy_hold_same_exposure"}
    assert all("test_stress_2x_cost" in f["results"]["B_agent"] for f in report["folds"])


def test_future_mutation_does_not_change_earlier_fold():
    original = demo_daily(520)
    a = compare_agents(original, Settings(), MockAgent(), annual_periods=365)
    altered = original.copy()
    altered.loc[400:, ["open", "high", "low", "close"]] *= 1.2
    b = compare_agents(altered, Settings(), MockAgent(), annual_periods=365)
    assert a["folds"][0]["results"] == b["folds"][0]["results"]
    assert a["folds"][0]["proposals"] == b["folds"][0]["proposals"]


def test_live_endpoint_is_not_called_during_experiment(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Unexpected broker or HTTP call")
    monkeypatch.setattr("requests.get", fail)
    monkeypatch.setattr("requests.post", fail)
    compare_agents(demo_daily(520), Settings(), MockAgent(), annual_periods=365)


def test_local_llm_rejects_redirect_without_following(monkeypatch):
    class FakeResponse:
        status_code = 302
        def raise_for_status(self):
            return None
    class FakeSession:
        trust_env = True
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def post(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            assert self.trust_env is False
            return FakeResponse()
    monkeypatch.setattr("requests.Session", FakeSession)
    agent = LocalLLMAgent("http://127.0.0.1:8000/v1", "model")
    with pytest.raises(AgentError, match="redirect"):
        agent.propose({"bar_count": 500})


def test_twice_costs_reduce_returns_when_orders_are_made():
    report = compare_agents(demo_daily(520), Settings(), MockAgent(), annual_periods=365)
    for group, stats in report["out_of_sample_summary"].items():
        if stats["total_orders"] > 0:
            assert stats["compounded_return_2x_cost"] <= stats["compounded_return_across_four_nonoverlapping_test_windows"] + 1e-12, group
