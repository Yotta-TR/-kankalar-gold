"""Validated configuration constrained to research-only operation."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppSettings(StrictModel):
    name: str = "KANKALAR GOLD AI"
    environment: Literal["research"] = "research"
    safe_mode: Literal[True] = True
    timezone_storage: Literal["UTC"] = "UTC"
    language: str = "ar"


class InstrumentSettings(StrictModel):
    logical_name: str = "GOLD_USD"
    broker_symbol: str | None = None
    source: Literal["mock"] = "mock"


class TimeframeSettings(StrictModel):
    context: tuple[str, ...] = ("D1", "H4")
    bias: tuple[str, ...] = ("H1",)
    setup: tuple[str, ...] = ("M15",)
    confirmation: tuple[str, ...] = ("M5",)
    diagnostics: tuple[str, ...] = ("M1",)


class SignalSettings(StrictModel):
    allow_live_trading: Literal[False] = False
    require_closed_bars: bool = True
    allow_probability_display: bool = False
    duplicate_suppression: bool = True
    default_no_trade_on_uncertainty: bool = True


class RiskSettings(StrictModel):
    mode: Literal["virtual"] = "virtual"
    max_risk_per_trade: float | None = Field(default=None, gt=0)
    max_daily_loss: float | None = Field(default=None, gt=0)
    max_drawdown: float | None = Field(default=None, gt=0)


class ModelSettings(StrictModel):
    baseline: str = "logistic_regression"
    tree_candidates: tuple[str, ...] = ("random_forest", "xgboost", "lightgbm")
    advanced_regime_candidates: tuple[str, ...] = ("hmm", "markov_switching")
    deep_learning_enabled: bool = False


class NotificationSettings(StrictModel):
    telegram_enabled: Literal[False] = False


class ProjectConfig(StrictModel):
    app: AppSettings = Field(default_factory=AppSettings)
    instrument: InstrumentSettings = Field(default_factory=InstrumentSettings)
    timeframes: TimeframeSettings = Field(default_factory=TimeframeSettings)
    signals: SignalSettings = Field(default_factory=SignalSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)


def load_config(path: str | Path) -> ProjectConfig:
    """Load YAML and validate every field against the Phase 0 safety boundary."""
    with Path(path).open(encoding="utf-8") as config_file:
        raw_config = yaml.safe_load(config_file)
    if not isinstance(raw_config, dict):
        raise ValueError("configuration root must be a YAML mapping")
    return ProjectConfig.model_validate(raw_config)
