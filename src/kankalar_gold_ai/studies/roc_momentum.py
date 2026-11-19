"""Descriptive percentage rate of change for one explicitly selected horizon."""

from __future__ import annotations

import math
from collections.abc import Mapping

from kankalar_gold_ai.core.contracts import Direction, MarketBar, StudyStatus
from kankalar_gold_ai.studies.base import ConfigValue, StudyOutput
from kankalar_gold_ai.studies.errors import StudyConfigError


class ROCMomentumStudy:
    study_id = "trend.roc_momentum"
    version = "0.1.0"
    required_timeframes = frozenset({"M1", "M5", "M15", "H1", "H4", "D1"})

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        if set(raw) != {"roc_period"}:
            raise StudyConfigError("configuration requires exactly 'roc_period'")
        period = raw["roc_period"]
        if type(period) is not int or period < 1:
            raise StudyConfigError("'roc_period' must be a positive built-in integer")
        return {"roc_period": period}

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        period = self.validate_config(config)["roc_period"]
        assert type(period) is int
        return period + 1

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        period = self.validate_config(config)["roc_period"]
        assert type(period) is int
        if len(bars) < period + 1:
            raise ValueError("not enough bars to compute ROC")
        if any(not math.isfinite(bar.close) or bar.close <= 0 for bar in bars):
            raise ArithmeticError("ROC requires finite positive closes")
        current, reference = bars[-1].close, bars[-period - 1].close
        value = 100.0 * (current / reference - 1.0)
        if not math.isfinite(value):
            raise ArithmeticError("ROC arithmetic is not finite")
        state, direction = (
            ("POSITIVE", Direction.BULLISH)
            if value > 0
            else ("NEGATIVE", Direction.BEARISH)
            if value < 0
            else ("ZERO", Direction.NEUTRAL)
        )
        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state=state,
            value=value,
            normalized_value=None,
            direction=direction,
            evidence=(
                f"roc_period={period}",
                f"current_close={current!r}",
                f"reference_close={reference!r}",
                "ROC = 100 * (current close / period-ago close - 1).",
            ),
            limitations=(
                "ROC is descriptive momentum, not a validated strategy or regime interpretation.",
                "Each execution describes only its configured horizon; "
                "no normalization is assumed.",
            ),
        )
