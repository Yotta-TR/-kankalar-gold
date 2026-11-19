"""Descriptive position relative to a prior completed-bar Donchian channel."""

from __future__ import annotations

import math
from collections.abc import Mapping

from kankalar_gold_ai.core.contracts import Direction, MarketBar, StudyStatus
from kankalar_gold_ai.studies.base import ConfigValue, StudyOutput
from kankalar_gold_ai.studies.errors import StudyConfigError


class DonchianStudy:
    study_id = "trend.donchian"
    version = "0.1.0"
    required_timeframes = frozenset({"M1", "M5", "M15", "H1", "H4", "D1"})

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        if set(raw) != {"donchian_period"}:
            raise StudyConfigError("configuration requires exactly 'donchian_period'")
        period = raw["donchian_period"]
        if type(period) is not int or period < 1:
            raise StudyConfigError("'donchian_period' must be a positive built-in integer")
        return {"donchian_period": period}

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        period = self.validate_config(config)["donchian_period"]
        assert type(period) is int
        return period + 1

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        period = self.validate_config(config)["donchian_period"]
        assert type(period) is int
        if len(bars) < period + 1:
            raise ValueError("not enough bars to compute the prior Donchian channel")
        window = bars[-period - 1 : -1]
        current = bars[-1].close
        used = [current, *(bar.high for bar in window), *(bar.low for bar in window)]
        if any(not math.isfinite(value) or value <= 0 for value in used):
            raise ArithmeticError("Donchian requires finite positive prices")
        upper = max(bar.high for bar in window)
        lower = min(bar.low for bar in window)
        width = upper - lower
        numerator = current - lower
        if not math.isfinite(width) or not math.isfinite(numerator):
            raise ArithmeticError("Donchian arithmetic is not finite")
        position = None if width == 0 else numerator / width
        if position is not None and not math.isfinite(position):
            raise ArithmeticError("Donchian position is not finite")
        if current > upper:
            state, direction, comparison = "BREAKOUT_UP", Direction.BULLISH, "above_upper"
        elif current < lower:
            state, direction, comparison = "BREAKOUT_DOWN", Direction.BEARISH, "below_lower"
        else:
            state = "ZERO_WIDTH_CHANNEL" if width == 0 else "INSIDE_CHANNEL"
            direction, comparison = Direction.NEUTRAL, "within_or_equal_boundaries"
        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state=state,
            value=position,
            direction=direction,
            evidence=(
                f"donchian_period={period}",
                f"upper_channel={upper!r}",
                f"lower_channel={lower!r}",
                f"channel_width={width!r}",
                f"current_close={current!r}",
                f"strict_comparison={comparison}",
            ),
            limitations=(
                "Donchian states are descriptive, not a validated strategy "
                "or regime interpretation.",
                "The channel excludes the current bar and position is not clipped.",
            ),
        )
