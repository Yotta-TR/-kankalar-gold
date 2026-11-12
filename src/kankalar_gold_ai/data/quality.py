"""Conservative data quality checks independent of a broker or calendar vendor."""

import math
from datetime import datetime, timedelta
from numbers import Real
from typing import Callable

from kankalar_gold_ai.core.contracts import (
    DataQualityReport,
    DataStatus,
    MarketBar,
    MarketQuote,
    as_utc,
)

TIMEFRAME_DURATIONS = {
    "M1": timedelta(minutes=1),
    "M5": timedelta(minutes=5),
    "M15": timedelta(minutes=15),
    "H1": timedelta(hours=1),
    "H4": timedelta(hours=4),
    "D1": timedelta(days=1),
}


def _finite_real(value: object, *, positive: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(numeric) and (numeric > 0 if positive else numeric >= 0)


def validate_quote(
    quote: MarketQuote, now: datetime, max_age: timedelta, max_spread: float
) -> DataQualityReport:
    now = as_utc(now)
    if max_age < timedelta(0) or not math.isfinite(max_spread) or max_spread < 0:
        raise ValueError("max_age and max_spread must be non-negative and finite")
    if not math.isfinite(quote.bid) or not math.isfinite(quote.ask):
        return DataQualityReport(DataStatus.ERROR, now, ("NON_FINITE_QUOTE",))
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return DataQualityReport(DataStatus.ERROR, now, ("INVALID_BID_ASK",))
    if quote.last is not None and (not math.isfinite(quote.last) or quote.last < 0):
        return DataQualityReport(DataStatus.ERROR, now, ("INVALID_LAST",))
    if quote.timestamp_milliseconds is not None:
        second = math.floor(quote.timestamp_utc.timestamp())
        if not second * 1000 <= quote.timestamp_milliseconds < (second + 1) * 1000:
            return DataQualityReport(DataStatus.ERROR, now, ("INCONSISTENT_TICK_TIMESTAMP",))
    if quote.timestamp_utc > now:
        return DataQualityReport(DataStatus.ERROR, now, ("FUTURE_QUOTE",))
    if now - quote.timestamp_utc > max_age:
        return DataQualityReport(DataStatus.STALE, now, ("STALE_QUOTE",))
    if quote.spread > max_spread:
        return DataQualityReport(DataStatus.ERROR, now, ("ABNORMAL_SPREAD",))
    return DataQualityReport(DataStatus.VALID, now, ())


def validate_bars(
    bars: tuple[MarketBar, ...],
    timeframe: str,
    now: datetime,
    *,
    expected_open: Callable[[datetime], bool] | None = None,
) -> DataQualityReport:
    """Only classify gaps as missing when a caller supplies a session calendar."""
    now = as_utc(now)
    if not isinstance(timeframe, str) or timeframe not in TIMEFRAME_DURATIONS:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    if not bars:
        return DataQualityReport(DataStatus.INSUFFICIENT_DATA, now, ("EMPTY_HISTORY",))
    duration = TIMEFRAME_DURATIONS[timeframe]
    reasons: set[str] = set()
    seen: set[datetime] = set()
    previous: datetime | None = None
    instrument = bars[0].instrument
    source = bars[0].source
    for bar in bars:
        timestamp = bar.timestamp_utc
        if bar.instrument != instrument:
            reasons.add("INSTRUMENT_MISMATCH")
        if bar.source != source:
            reasons.add("SOURCE_MISMATCH")
        if bar.timeframe != timeframe:
            reasons.add("TIMEFRAME_MISMATCH")
        if timestamp in seen:
            reasons.add("DUPLICATE_BAR")
        if previous is not None and timestamp < previous:
            reasons.add("OUT_OF_ORDER")
        if previous is not None and timestamp > previous + duration:
            if expected_open is None:
                reasons.add("GAP_SESSION_UNKNOWN")
            else:
                if (timestamp - previous) // duration > 100_000:
                    raise ValueError("gap exceeds the supported calendar-check bound")
                missing = previous + duration
                while missing < timestamp:
                    if expected_open(missing):
                        reasons.add("MISSING_BAR")
                        break
                    missing += duration
        seen.add(timestamp)
        previous = timestamp
        if timestamp + duration > now:
            reasons.add("INCOMPLETE_BAR")
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
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                reasons.add(f"INVALID_{field.upper()}")
    errors = {
        "DUPLICATE_BAR",
        "OUT_OF_ORDER",
        "INVALID_PRICE",
        "INVALID_OHLC",
        "TIMEFRAME_MISMATCH",
        "INSTRUMENT_MISMATCH",
        "SOURCE_MISMATCH",
        "INVALID_VOLUME",
        "INVALID_TICK_VOLUME",
        "INVALID_REAL_VOLUME",
        "INVALID_SPREAD_POINTS",
    }
    if reasons & errors:
        return DataQualityReport(DataStatus.ERROR, now, tuple(sorted(reasons)))
    if reasons:
        return DataQualityReport(DataStatus.INSUFFICIENT_DATA, now, tuple(sorted(reasons)))
    return DataQualityReport(DataStatus.VALID, now, ())
