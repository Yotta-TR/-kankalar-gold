"""Descriptive Average True Range over validated completed bars, one timeframe per run."""

from __future__ import annotations

import math
from collections.abc import Mapping

from kankalar_gold_ai.core.contracts import Direction, MarketBar, StudyStatus
from kankalar_gold_ai.studies.base import ConfigValue, StudyOutput
from kankalar_gold_ai.studies.errors import StudyConfigError


class ATRStudy:
    """Wilder ATR with an omitted first-bar True Range observation."""

    study_id = "volatility.atr"
    version = "0.1.0"
    required_timeframes = frozenset({"M1", "M5", "M15", "H1", "H4", "D1"})

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        if set(raw) != {"atr_period"}:
            if "atr_period" not in raw:
                raise StudyConfigError("configuration requires 'atr_period'")
            raise StudyConfigError("unknown ATR configuration key")
        period = raw["atr_period"]
        if type(period) is not int or period < 1:
            raise StudyConfigError("'atr_period' must be a positive built-in integer")
        return {"atr_period": period}

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        normalized = self.validate_config(config)
        period = normalized["atr_period"]
        if type(period) is not int:
            raise StudyConfigError("'atr_period' must be a positive built-in integer")
        return period + 1

    def compute(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, ConfigValue],
    ) -> StudyOutput:
        normalized = self.validate_config(config)
        period = normalized["atr_period"]
        if type(period) is not int:
            raise StudyConfigError("'atr_period' must be a positive built-in integer")
        if len(bars) < period + 1:
            raise ValueError("not enough bars to compute ATR")

        true_ranges: list[float] = []
        for previous, current in zip(bars, bars[1:]):
            true_range = max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
            if not math.isfinite(true_range) or true_range < 0:
                raise ArithmeticError("True Range is not finite and non-negative")
            true_ranges.append(true_range)

        initial = math.fsum(true_ranges[:period]) / period
        if not math.isfinite(initial) or initial < 0:
            raise ArithmeticError("ATR seed is not finite and non-negative")
        atr = initial
        for true_range in true_ranges[period:]:
            numerator = (period - 1) * atr + true_range
            if not math.isfinite(numerator):
                raise ArithmeticError("ATR smoothing arithmetic is not finite")
            atr = numerator / period
            if not math.isfinite(atr) or atr < 0:
                raise ArithmeticError("ATR is not finite and non-negative")

        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state=None,
            value=atr,
            normalized_value=None,
            direction=Direction.NOT_APPLICABLE,
            uncertainty=None,
            evidence=(
                f"atr_period={period}",
                "True Range is max(high - low, abs(high - previous close), "
                "abs(low - previous close)).",
                "The first supplied bar has no True Range observation; subsequent "
                "observations use the immediately preceding supplied bar close.",
                "ATR uses Wilder smoothing seeded by the arithmetic mean of the "
                "first atr_period True Range observations.",
            ),
            limitations=(
                "ATR is a descriptive volatility measurement; it does not predict "
                "direction or recommend trades.",
            ),
        )
