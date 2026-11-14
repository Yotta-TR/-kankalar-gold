"""Descriptive SMA ordering and fast-SMA slope on completed bars."""

from __future__ import annotations

import math
from collections.abc import Mapping

from kankalar_gold_ai.core.contracts import Direction, MarketBar, StudyStatus
from kankalar_gold_ai.studies.base import ConfigValue, StudyOutput
from kankalar_gold_ai.studies.errors import StudyConfigError

_CONFIG_KEYS = frozenset(
    {
        "fast_period",
        "middle_period",
        "slow_period",
        "slope_lookback",
        "flat_slope_fraction_per_bar",
    }
)


def _parameters(raw: Mapping[str, ConfigValue]) -> tuple[int, int, int, int, float]:
    if set(raw) != _CONFIG_KEYS:
        raise StudyConfigError("configuration must contain exactly the five required keys")
    fast = raw["fast_period"]
    middle = raw["middle_period"]
    slow = raw["slow_period"]
    lookback = raw["slope_lookback"]
    if any(type(value) is not int or value < 1 for value in (fast, middle, slow, lookback)):
        raise StudyConfigError("periods and slope_lookback must be positive built-in integers")
    assert type(fast) is int and type(middle) is int and type(slow) is int
    assert type(lookback) is int
    if not fast < middle < slow:
        raise StudyConfigError("periods must satisfy fast_period < middle_period < slow_period")
    threshold = raw["flat_slope_fraction_per_bar"]
    if type(threshold) is not int and type(threshold) is not float:
        raise StudyConfigError("flat_slope_fraction_per_bar must be a finite non-negative number")
    try:
        epsilon = float(threshold)
    except OverflowError as error:
        raise StudyConfigError("flat_slope_fraction_per_bar must be finite") from error
    if not math.isfinite(epsilon) or epsilon < 0:
        raise StudyConfigError("flat_slope_fraction_per_bar must be a finite non-negative number")
    return fast, middle, slow, lookback, epsilon


class MASlopeOrderStudy:
    """Three SMA ordering and a fractional fast-SMA slope, without prediction."""

    study_id = "trend.ma_slope_order"
    version = "0.1.0"
    required_timeframes = frozenset({"M1", "M5", "M15", "H1", "H4", "D1"})

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        fast, middle, slow, lookback, epsilon = _parameters(raw)
        return {
            "fast_period": fast,
            "middle_period": middle,
            "slow_period": slow,
            "slope_lookback": lookback,
            "flat_slope_fraction_per_bar": epsilon,
        }

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        fast, _middle, slow, lookback, _epsilon = _parameters(config)
        return max(slow, fast + lookback)

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        fast, middle, slow, lookback, epsilon = _parameters(config)
        if len(bars) < max(slow, fast + lookback):
            raise ValueError("not enough bars to compute SMA ordering and slope")

        def sma(period: int, end: int) -> float:
            average = math.fsum(bar.close for bar in bars[end - period : end]) / period
            if not math.isfinite(average) or average <= 0:
                raise ArithmeticError("SMA must be positive and finite")
            return average

        end = len(bars)
        fast_now = sma(fast, end)
        middle_now = sma(middle, end)
        slow_now = sma(slow, end)
        fast_previous = sma(fast, end - lookback)
        delta = fast_now - fast_previous
        if not math.isfinite(delta):
            raise ArithmeticError("SMA difference is not finite")
        raw_slope = delta / lookback
        fractional_change = delta / fast_previous
        normalized_slope = fractional_change / lookback
        if not all(
            math.isfinite(value) for value in (raw_slope, fractional_change, normalized_slope)
        ):
            raise ArithmeticError("SMA slope is not finite")

        if fast_now > middle_now > slow_now:
            ordering = "UP"
        elif fast_now < middle_now < slow_now:
            ordering = "DOWN"
        else:
            ordering = "MIXED"
        slope = (
            "UP"
            if normalized_slope > epsilon
            else ("DOWN" if normalized_slope < -epsilon else "FLAT")
        )
        state = f"ORDER_{ordering}_SLOPE_{slope}"
        direction = (
            Direction.BULLISH
            if state == "ORDER_UP_SLOPE_UP"
            else Direction.BEARISH
            if state == "ORDER_DOWN_SLOPE_DOWN"
            else Direction.NEUTRAL
        )
        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state=state,
            value=raw_slope,
            normalized_value=normalized_slope,
            direction=direction,
            uncertainty=None,
            evidence=(
                "Ordering compares the current fast, middle and slow simple moving averages.",
                "Fast SMA slope is its change over the configured lookback divided by "
                "observed bars; normalized slope divides the change by the previous fast SMA.",
            ),
            limitations=(
                "This lagging, parameter-sensitive description does not predict direction "
                "or recommend trades.",
                "NEUTRAL means ordering and slope are not jointly aligned; it does not "
                "assert a sideways market.",
            ),
        )
