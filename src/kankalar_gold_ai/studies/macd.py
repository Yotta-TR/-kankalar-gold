"""Descriptive MACD histogram over completed closes."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from kankalar_gold_ai.core.contracts import Direction, MarketBar, StudyStatus
from kankalar_gold_ai.studies.base import ConfigValue, StudyOutput
from kankalar_gold_ai.studies.errors import StudyConfigError


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ArithmeticError("MACD arithmetic is not finite")
    return value


def _ema(values: Sequence[float], period: int) -> list[float]:
    result = [_finite(math.fsum(values[:period]) / period)]
    alpha = 2.0 / (period + 1)
    for value in values[period:]:
        difference = _finite(value - result[-1])
        increment = _finite(alpha * difference)
        result.append(_finite(result[-1] + increment))
    return result


class MACDStudy:
    study_id = "trend.macd"
    version = "0.1.0"
    required_timeframes = frozenset({"M1", "M5", "M15", "H1", "H4", "D1"})

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        keys = ("fast_period", "slow_period", "signal_period")
        if set(raw) != set(keys):
            raise StudyConfigError(
                "configuration requires exactly fast_period, slow_period, signal_period"
            )
        periods: dict[str, int] = {}
        for key in keys:
            value = raw[key]
            if type(value) is not int or value < 1:
                raise StudyConfigError(f"{key!r} must be a positive built-in integer")
            periods[key] = value
        if periods["fast_period"] >= periods["slow_period"]:
            raise StudyConfigError("fast_period must be less than slow_period")
        return periods

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        periods = self.validate_config(config)
        slow, signal = periods["slow_period"], periods["signal_period"]
        assert type(slow) is int and type(signal) is int
        return slow + signal

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        periods = self.validate_config(config)
        fast, slow, signal = (
            periods["fast_period"],
            periods["slow_period"],
            periods["signal_period"],
        )
        assert type(fast) is int and type(slow) is int and type(signal) is int
        if len(bars) < slow + signal:
            raise ValueError("not enough bars to compute MACD and previous histogram")
        closes = [bar.close for bar in bars]
        if any(not math.isfinite(close) or close <= 0 for close in closes):
            raise ArithmeticError("MACD requires finite positive closes")
        fast_ema, slow_ema = _ema(closes, fast), _ema(closes, slow)
        macd = [
            _finite(fast_value - slow_value)
            for fast_value, slow_value in zip(fast_ema[slow - fast :], slow_ema)
        ]
        signal_ema = _ema(macd, signal)
        previous = _finite(macd[-2] - signal_ema[-2])
        current = _finite(macd[-1] - signal_ema[-1])
        change = _finite(current - previous)
        relation = "positive" if macd[-1] > 0 else "negative" if macd[-1] < 0 else "zero"
        state, direction = (
            ("HISTOGRAM_POSITIVE", Direction.BULLISH)
            if current > 0
            else ("HISTOGRAM_NEGATIVE", Direction.BEARISH)
            if current < 0
            else ("HISTOGRAM_ZERO", Direction.NEUTRAL)
        )
        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state=state,
            value=current,
            direction=direction,
            evidence=(
                f"fast_period={fast}",
                f"slow_period={slow}",
                f"signal_period={signal}",
                f"macd_line={macd[-1]!r}",
                f"signal_line={signal_ema[-1]!r}",
                f"histogram={current!r}",
                f"previous_histogram={previous!r}",
                f"histogram_change={change!r}",
                f"macd_zero_line_relation={relation}",
            ),
            limitations=(
                "MACD is descriptive, not a validated strategy or regime interpretation.",
                "The EMA seed depends on supplied history; only histogram is a structured value.",
            ),
        )
