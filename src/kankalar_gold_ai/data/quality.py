"""Conservative data quality checks independent of a broker or calendar vendor."""

import math
from datetime import datetime, timedelta
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
    if timeframe not in TIMEFRAME_DURATIONS:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    if not bars:
        return DataQualityReport(DataStatus.INSUFFICIENT_DATA, now, ("EMPTY_HISTORY",))
    duration = TIMEFRAME_DURATIONS[timeframe]
    reasons: set[str] = set()
    seen: set[datetime] = set()
    previous: datetime | None = None
    for bar in bars:
        timestamp = bar.timestamp_utc
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
        if any(
            not math.isfinite(value) or value <= 0
            for value in (bar.open, bar.high, bar.low, bar.close)
        ):
            reasons.add("INVALID_PRICE")
        elif not (bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high):
            reasons.add("INVALID_OHLC")
    errors = {
        "DUPLICATE_BAR",
        "OUT_OF_ORDER",
        "INVALID_PRICE",
        "INVALID_OHLC",
        "TIMEFRAME_MISMATCH",
    }
    if reasons & errors:
        return DataQualityReport(DataStatus.ERROR, now, tuple(sorted(reasons)))
    if reasons:
        return DataQualityReport(DataStatus.INSUFFICIENT_DATA, now, tuple(sorted(reasons)))
    return DataQualityReport(DataStatus.VALID, now, ())
