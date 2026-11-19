"""Descriptive Wilder RSI and its one-completed-bar movement."""

from __future__ import annotations

import math
from collections.abc import Mapping

from kankalar_gold_ai.core.contracts import Direction, MarketBar, StudyStatus
from kankalar_gold_ai.studies.base import ConfigValue, StudyOutput
from kankalar_gold_ai.studies.errors import StudyConfigError


class RSIStudy:
    study_id = "trend.rsi"
    version = "0.1.0"
    required_timeframes = frozenset({"M1", "M5", "M15", "H1", "H4", "D1"})

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        if set(raw) != {"rsi_period"}:
            raise StudyConfigError("configuration requires exactly 'rsi_period'")
        period = raw["rsi_period"]
        if type(period) is not int or period < 2:
            raise StudyConfigError("'rsi_period' must be a built-in integer of at least 2")
        return {"rsi_period": period}

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        period = self.validate_config(config)["rsi_period"]
        assert type(period) is int
        return period + 2

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        period = self.validate_config(config)["rsi_period"]
        assert type(period) is int
        if len(bars) < period + 2:
            raise ValueError("not enough bars to compute RSI and its slope")
        if any(not math.isfinite(bar.close) or bar.close <= 0 for bar in bars):
            raise ArithmeticError("RSI requires finite positive closes")
        changes = [current.close - previous.close for previous, current in zip(bars, bars[1:])]
        if any(not math.isfinite(change) for change in changes):
            raise ArithmeticError("RSI close change is not finite")
        gains = [max(change, 0.0) for change in changes]
        losses = [max(-change, 0.0) for change in changes]
        gain = math.fsum(gains[:period]) / period
        loss = math.fsum(losses[:period]) / period

        def level(avg_gain: float, avg_loss: float) -> float:
            if avg_gain == 0:
                return 50.0 if avg_loss == 0 else 0.0
            if avg_loss == 0:
                return 100.0
            scale = max(avg_gain, avg_loss)
            scaled_gain = avg_gain / scale
            scaled_loss = avg_loss / scale
            return 100.0 * scaled_gain / (scaled_gain + scaled_loss)

        current = level(gain, loss)
        previous = current
        for next_gain, next_loss in zip(gains[period:], losses[period:]):
            previous = current
            gain = gain * ((period - 1) / period) + next_gain / period
            loss = loss * ((period - 1) / period) + next_loss / period
            if not math.isfinite(gain) or not math.isfinite(loss):
                raise ArithmeticError("RSI smoothing arithmetic is not finite")
            current = level(gain, loss)
        movement = current - previous
        state, direction = (
            ("RSI_RISING", Direction.BULLISH)
            if movement > 0
            else ("RSI_FALLING", Direction.BEARISH)
            if movement < 0
            else ("RSI_FLAT", Direction.NEUTRAL)
        )
        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state=state,
            value=current,
            normalized_value=movement / 100.0,
            direction=direction,
            evidence=(
                f"rsi_period={period}",
                f"previous_rsi={previous!r}",
                "Wilder averages are seeded by the first period close-to-close gains and losses.",
            ),
            limitations=(
                "RSI movement is descriptive, not a validated strategy or regime interpretation.",
                "The result depends on the supplied history used to seed Wilder smoothing.",
            ),
        )
