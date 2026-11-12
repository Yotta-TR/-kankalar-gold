"""Intrinsic validation shared by historical bars and snapshot serialization."""

import math
from datetime import datetime
from numbers import Real

from kankalar_gold_ai.core.contracts import MarketBar, as_utc
from kankalar_gold_ai.data.quality import TIMEFRAME_DURATIONS


def _finite_real(value: object, *, positive: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(numeric) and (numeric > 0 if positive else numeric >= 0)


def bar_integrity_reasons(
    bars: tuple[object, ...],
    timeframe: str,
    expected_instrument: str | None = None,
) -> tuple[str, ...]:
    """Return intrinsic consistency errors without making session or time-completeness claims."""
    reasons: set[str] = set()
    if not bars:
        return ()

    first = bars[0]
    if not isinstance(first, MarketBar):
        return ("INVALID_BAR_TYPE",)
    instrument = first.instrument
    source = first.source
    previous: datetime | None = None
    seen: set[datetime] = set()

    for bar in bars:
        if not isinstance(bar, MarketBar):
            reasons.add("INVALID_BAR_TYPE")
            continue
        if not isinstance(bar.instrument, str) or not bar.instrument.strip():
            reasons.add("INVALID_INSTRUMENT")
        elif bar.instrument != instrument:
            reasons.add("INSTRUMENT_MISMATCH")
        if expected_instrument is not None and bar.instrument != expected_instrument:
            reasons.add("INSTRUMENT_MISMATCH")
        if not isinstance(bar.source, str) or not bar.source.strip():
            reasons.add("INVALID_SOURCE")
        elif bar.source != source:
            reasons.add("SOURCE_MISMATCH")
        if not isinstance(bar.timeframe, str) or bar.timeframe not in TIMEFRAME_DURATIONS:
            reasons.add("TIMEFRAME_MISMATCH")
        elif bar.timeframe != timeframe:
            reasons.add("TIMEFRAME_MISMATCH")

        try:
            timestamp = as_utc(bar.timestamp_utc)
        except (AttributeError, TypeError, ValueError):
            reasons.add("INVALID_TIMESTAMP")
        else:
            if timestamp in seen:
                reasons.add("DUPLICATE_BAR")
            if previous is not None and timestamp < previous:
                reasons.add("OUT_OF_ORDER")
            seen.add(timestamp)
            previous = timestamp

        prices = (bar.open, bar.high, bar.low, bar.close)
        if not all(_finite_real(value, positive=True) for value in prices):
            reasons.add("INVALID_PRICE")
        else:
            open_price, high, low, close = (float(value) for value in prices)
            if not (low <= min(open_price, close) <= max(open_price, close) <= high):
                reasons.add("INVALID_OHLC")

        if bar.volume is not None and not _finite_real(bar.volume):
            reasons.add("INVALID_VOLUME")
        for field, value in (
            ("tick_volume", bar.tick_volume),
            ("real_volume", bar.real_volume),
            ("spread_points", bar.spread_points),
        ):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                reasons.add(f"INVALID_{field.upper()}")
            elif value is not None and value < 0:
                reasons.add(f"INVALID_{field.upper()}")

    if not isinstance(instrument, str) or not instrument.strip():
        reasons.add("INVALID_INSTRUMENT")
    if not isinstance(source, str) or not source.strip():
        reasons.add("INVALID_SOURCE")
    if not isinstance(timeframe, str) or timeframe not in TIMEFRAME_DURATIONS:
        reasons.add("TIMEFRAME_MISMATCH")
    return tuple(sorted(reasons))
