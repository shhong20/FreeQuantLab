from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    fee_bps: float = 10.0
    slippage_bps: float = 5.0
    train_fraction: float = 0.60
    validation_fraction: float = 0.20
    max_drawdown: float = 0.25
    min_test_sharpe: float = 0.25
    min_test_orders: int = 3
    paper_min_days: int = 30
    paper_min_orders: int = 8
    initial_cash: float = 1_000_000
    max_position_fraction: float = 0.20
    max_paper_daily_loss: float = 0.03

    def __post_init__(self) -> None:
        if not (0.4 <= self.train_fraction <= 0.8):
            raise ValueError("train_fraction must be in [0.4, 0.8]")
        if not (0.1 <= self.validation_fraction <= 0.3):
            raise ValueError("validation_fraction must be in [0.1, 0.3]")
        if self.train_fraction + self.validation_fraction >= 0.9:
            raise ValueError("Leave at least 10% for untouched test period")
        if any(x < 0 for x in (self.fee_bps, self.slippage_bps, self.min_test_orders, self.paper_min_days, self.paper_min_orders)):
            raise ValueError("negative costs/counts are invalid")
        if not (0 < self.max_drawdown <= 1 and 0 < self.max_position_fraction <= 1):
            raise ValueError("invalid risk limits")
        if not (0 < self.max_paper_daily_loss < 1) or self.initial_cash <= 0:
            raise ValueError("invalid cash/daily loss")


def load_config(path: str | Path) -> tuple[Settings, list[dict]]:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    allowed = {f.name for f in fields(Settings)}
    settings = Settings(**{k: v for k, v in d.items() if k in allowed})
    assets = d.get("assets", [])
    if not isinstance(assets, list):
        raise ValueError("assets must be a list")
    for asset in assets:
        if asset.get("provider") not in {"upbit", "stooq", "kis", "naver", "csv", "demo"}:
            raise ValueError(f"Unsupported provider: {asset.get('provider')}")
    return settings, assets
