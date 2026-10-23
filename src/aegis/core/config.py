"""Typed configuration. Values come from defaults + environment; secrets are never stored here."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt


class TradingMode(StrEnum):
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"


class RiskLimits(BaseModel):
    """Hard limits enforced by the risk engine. Increasing any value needs owner approval."""

    model_config = ConfigDict(frozen=True)

    risk_per_trade_pct: PositiveFloat = Field(0.5, le=2.0, description="% of equity risked to SL")
    max_lots_per_order: PositiveFloat = Field(0.10, le=5.0)
    max_open_positions: PositiveInt = Field(3, le=20)
    max_lots_per_symbol: PositiveFloat = Field(0.20, le=10.0)
    max_total_notional_pct: PositiveFloat = Field(300.0, description="Sum of notional / equity, %")
    daily_loss_limit_pct: PositiveFloat = Field(2.0, le=10.0)
    max_signal_age_seconds: PositiveInt = 120
    max_entry_deviation_pips: PositiveFloat = 15.0
    min_stop_distance_pips: PositiveFloat = 5.0
    max_stop_distance_pips: PositiveFloat = 150.0
    dedupe_window_seconds: PositiveInt = 3600

    def is_increase_over(self, other: RiskLimits) -> list[str]:
        """Field names where self is *riskier* than other."""
        tighter_is_lower = {
            "risk_per_trade_pct",
            "max_lots_per_order",
            "max_open_positions",
            "max_lots_per_symbol",
            "max_total_notional_pct",
            "daily_loss_limit_pct",
            "max_signal_age_seconds",
            "max_entry_deviation_pips",
            "max_stop_distance_pips",
            "dedupe_window_seconds",
        }
        increases = []
        for name in type(self).model_fields:
            mine, theirs = getattr(self, name), getattr(other, name)
            if name in tighter_is_lower and mine > theirs:
                increases.append(name)
            elif name == "min_stop_distance_pips" and mine < theirs:
                increases.append(name)
        return increases


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_dir: Path = Path("./var")
    trading_mode: TradingMode = TradingMode.PAPER
    account_currency: str = "USD"
    symbol_allowlist: frozenset[str] = frozenset({"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"})
    telegram_source_allowlist: frozenset[int] = frozenset()
    risk: RiskLimits = RiskLimits()

    @classmethod
    def from_env(cls) -> Settings:
        sources = os.environ.get("AEGIS_TELEGRAM_SOURCES", "")
        symbols = os.environ.get("AEGIS_SYMBOLS", "")
        kwargs: dict[str, object] = {
            "data_dir": Path(os.environ.get("AEGIS_DATA_DIR", "./var")),
            "trading_mode": TradingMode(os.environ.get("AEGIS_TRADING_MODE", "paper").lower()),
            "telegram_source_allowlist": frozenset(int(s) for s in sources.split(",") if s.strip()),
        }
        if symbols.strip():
            kwargs["symbol_allowlist"] = frozenset(s.strip().upper() for s in symbols.split(","))
        return cls.model_validate(kwargs)
