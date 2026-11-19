"""Descriptive Wilder DMI/ADX over validated completed bars."""

from __future__ import annotations

import math
from collections.abc import Mapping

from kankalar_gold_ai.core.contracts import Direction, MarketBar, StudyStatus
from kankalar_gold_ai.studies.base import ConfigValue, StudyOutput
from kankalar_gold_ai.studies.errors import StudyConfigError

_CONFIG_KEYS = frozenset({"adx_period"})
_TIMEFRAMES = frozenset({"M1", "M5", "M15", "H1", "H4", "D1"})


def _period(raw: Mapping[str, ConfigValue]) -> int:
    if set(raw) != _CONFIG_KEYS:
        raise StudyConfigError("configuration must contain exactly 'adx_period'")
    period = raw["adx_period"]
    if type(period) is not int or period < 2:
        raise StudyConfigError("'adx_period' must be a built-in integer of at least 2")
    return period


def _finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ArithmeticError(f"{name} is not finite")
    return value


def _nonnegative(value: float, name: str) -> float:
    _finite(value, name)
    if value < 0:
        raise ArithmeticError(f"{name} is negative")
    return value


def _bounded(value: float, name: str, maximum: float) -> float:
    _nonnegative(value, name)
    if value > maximum:
        raise ArithmeticError(f"{name} exceeds its mathematical bound")
    return value


def _metrics(
    plus_dm: float, minus_dm: float, true_range: float
) -> tuple[float, float, float, float | None, str, Direction, str]:
    plus_dm = _nonnegative(plus_dm, "smoothed +DM")
    minus_dm = _nonnegative(minus_dm, "smoothed -DM")
    true_range = _nonnegative(true_range, "smoothed TR")

    if true_range == 0:
        if plus_dm != 0 or minus_dm != 0:
            raise ArithmeticError("zero smoothed TR has non-zero smoothed DM")
        return 0.0, 0.0, 0.0, None, "DI_BALANCED", Direction.NEUTRAL, "ZERO_TR"

    plus_ratio = _nonnegative(plus_dm / true_range, "+DI ratio")
    minus_ratio = _nonnegative(minus_dm / true_range, "-DI ratio")
    if plus_ratio > 1 or minus_ratio > 1:
        raise ArithmeticError("smoothed DM exceeds smoothed TR")
    plus_di = _bounded(100.0 * plus_ratio, "+DI", 100.0)
    minus_di = _bounded(100.0 * minus_ratio, "-DI", 100.0)

    if plus_dm == 0 and minus_dm == 0:
        return plus_di, minus_di, 0.0, None, "DI_BALANCED", Direction.NEUTRAL, "ZERO_DM"

    scale = max(plus_dm, minus_dm)
    plus_scaled = plus_dm / scale
    minus_scaled = minus_dm / scale
    dominance = _finite(
        (plus_scaled - minus_scaled) / (plus_scaled + minus_scaled),
        "normalized DI dominance",
    )
    if not -1.0 <= dominance <= 1.0:
        raise ArithmeticError("normalized DI dominance exceeds its mathematical bound")
    dx = _bounded(100.0 * abs(dominance), "DX", 100.0)

    if plus_dm > minus_dm:
        return plus_di, minus_di, dx, dominance, "PLUS_DI_DOMINANT", Direction.BULLISH, "NONE"
    if minus_dm > plus_dm:
        return plus_di, minus_di, dx, dominance, "MINUS_DI_DOMINANT", Direction.BEARISH, "NONE"
    return plus_di, minus_di, dx, dominance, "DI_BALANCED", Direction.NEUTRAL, "NONE"


class ADXDMIStudy:
    """Wilder Directional Movement and ADX; one timeframe and no prediction."""

    study_id = "trend.adx_dm"
    version = "0.1.0"
    required_timeframes = _TIMEFRAMES

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        return {"adx_period": _period(raw)}

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        return 2 * _period(config)

    def compute(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, ConfigValue],
    ) -> StudyOutput:
        period = _period(config)
        if len(bars) < 2 * period:
            raise ValueError("not enough bars to compute ADX and directional movement")

        plus_observations: list[float] = []
        minus_observations: list[float] = []
        tr_observations: list[float] = []
        previous = bars[0]
        for current in bars[1:]:
            previous_high = float(previous.high)
            previous_low = float(previous.low)
            previous_close = float(previous.close)
            high = float(current.high)
            low = float(current.low)

            up_move = _finite(high - previous_high, "up move")
            down_move = _finite(previous_low - low, "down move")
            plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
            minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0

            true_range = max(
                _nonnegative(high - low, "high-low range"),
                _nonnegative(abs(high - previous_close), "high-close gap"),
                _nonnegative(abs(low - previous_close), "low-close gap"),
            )
            plus_observations.append(_nonnegative(plus_dm, "+DM observation"))
            minus_observations.append(_nonnegative(minus_dm, "-DM observation"))
            tr_observations.append(_nonnegative(true_range, "TR observation"))
            previous = current

        try:
            smoothed_plus = _nonnegative(math.fsum(plus_observations[:period]), "seed smoothed +DM")
            smoothed_minus = _nonnegative(
                math.fsum(minus_observations[:period]), "seed smoothed -DM"
            )
            smoothed_tr = _nonnegative(math.fsum(tr_observations[:period]), "seed smoothed TR")
        except OverflowError as error:
            raise ArithmeticError("Wilder seed arithmetic overflowed") from error

        plus_di, minus_di, dx, dominance, state, direction, zero_case = _metrics(
            smoothed_plus, smoothed_minus, smoothed_tr
        )
        dx_seed = [dx]
        adx: float | None = None

        for index in range(period, len(tr_observations)):
            smoothed_plus = _nonnegative(
                smoothed_plus - smoothed_plus / period + plus_observations[index],
                "smoothed +DM",
            )
            smoothed_minus = _nonnegative(
                smoothed_minus - smoothed_minus / period + minus_observations[index],
                "smoothed -DM",
            )
            smoothed_tr = _nonnegative(
                smoothed_tr - smoothed_tr / period + tr_observations[index],
                "smoothed TR",
            )
            plus_di, minus_di, dx, dominance, state, direction, zero_case = _metrics(
                smoothed_plus, smoothed_minus, smoothed_tr
            )
            if adx is None:
                dx_seed.append(dx)
                if len(dx_seed) == period:
                    try:
                        adx = _nonnegative(math.fsum(dx_seed) / period, "seed ADX")
                    except OverflowError as error:
                        raise ArithmeticError("ADX seed arithmetic overflowed") from error
            else:
                numerator = adx * (period - 1) + dx
                _nonnegative(numerator, "ADX recurrence numerator")
                adx = _bounded(numerator / period, "ADX", 100.0)

        if adx is None:
            raise ArithmeticError("ADX warm-up completed without an ADX value")
        _bounded(adx, "ADX", 100.0)
        assert dominance is None or -1.0 <= dominance <= 1.0

        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state=state,
            value=adx,
            normalized_value=dominance,
            direction=direction,
            uncertainty=None,
            evidence=(
                f"adx_period={period}",
                f"latest_adx={adx:.17g}",
                f"latest_plus_di={plus_di:.17g}",
                f"latest_minus_di={minus_di:.17g}",
                f"latest_dx={dx:.17g}",
                f"latest_smoothed_plus_dm={smoothed_plus:.17g}",
                f"latest_smoothed_minus_dm={smoothed_minus:.17g}",
                f"latest_smoothed_tr={smoothed_tr:.17g}",
                f"latest_zero_denominator_case={zero_case}",
                "ADX is the arithmetic mean of the first adx_period DX observations, "
                "then uses Wilder smoothing.",
                "Directional movement and True Range use adjacent supplied completed bars; "
                "the first supplied bar has no observation.",
            ),
            limitations=(
                "ADX is a lagging, parameter-sensitive description of directional "
                "movement strength; it does not predict direction or recommend trades.",
                "Direction reports only which smoothed DI dominates; it does not imply "
                "that ADX is strong. The shared output contract has no first-class +DI "
                "and -DI numeric fields.",
                "2 * adx_period bars is the first computable output, not a guarantee of "
                "convergence or stability.",
            ),
        )
