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
from kankalar_gold_ai.data.calendar import (
    SessionCalendar,
    local_day_completion_utc,
    validate_calendar_scope,
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


def bar_completion_utc(
    timestamp_utc: datetime,
    timeframe: str,
    calendar: SessionCalendar | None = None,
    *,
    timezone_name: str | None = None,
) -> datetime:
    """Return bar completion; calendar-scoped D1 bars use local calendar days."""
    timestamp = as_utc(timestamp_utc)
    if timeframe == "D1":
        if calendar is not None:
            return calendar.next_local_day_boundary(timestamp)
        if timezone_name is not None:
            return local_day_completion_utc(timestamp, timezone_name)
    try:
        return timestamp + TIMEFRAME_DURATIONS[timeframe]
    except KeyError as error:
        raise ValueError(f"unsupported timeframe: {timeframe}") from error


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
    calendar: SessionCalendar | None = None,
    expected_instrument: str | None = None,
) -> DataQualityReport:
    """Validate bars and classify gaps only with explicit, correctly scoped sessions.

    A calendar provider/instrument mismatch raises ``ValueError`` before any
    gap classification. Each complete half-open bar interval must be covered.
    Empty histories can check an explicit instrument scope, but cannot establish
    provider scope.
    """
    if calendar is not None:
        validate_calendar_scope(
            calendar,
            bars,
            expected_instrument=expected_instrument,
        )
    return _validate_bars(
        bars,
        timeframe,
        now,
        expected_open=expected_open,
        calendar=calendar,
        classify_gaps=True,
        expected_instrument=expected_instrument,
    )


def _validate_bars(
    bars: tuple[MarketBar, ...],
    timeframe: str,
    now: datetime,
    *,
    expected_open: Callable[[datetime], bool] | None,
    calendar: SessionCalendar | None,
    classify_gaps: bool,
    check_completion: bool = True,
    expected_instrument: str | None = None,
) -> DataQualityReport:
    now = as_utc(now)
    if expected_open is not None and calendar is not None:
        raise ValueError("provide calendar or expected_open, not both")
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
        if not isinstance(bar.instrument, str) or not bar.instrument.strip():
            reasons.add("INVALID_INSTRUMENT")
        if bar.instrument != instrument:
            reasons.add("INSTRUMENT_MISMATCH")
        if expected_instrument is not None and bar.instrument != expected_instrument:
            reasons.add("INSTRUMENT_MISMATCH")
        if bar.source != source:
            reasons.add("SOURCE_MISMATCH")
        if bar.timeframe != timeframe:
            reasons.add("TIMEFRAME_MISMATCH")
        if timestamp in seen:
            reasons.add("DUPLICATE_BAR")
        if previous is not None and timestamp < previous:
            reasons.add("OUT_OF_ORDER")
        previous_end = (
            None if previous is None else bar_completion_utc(previous, timeframe, calendar)
        )
        if classify_gaps and previous_end is not None and timestamp > previous_end:
            if calendar is not None:
                if (timestamp - previous_end) // duration > 100_000:
                    raise ValueError("gap exceeds the supported calendar-check bound")
                missing = previous_end
                while missing < timestamp:
                    missing_end = bar_completion_utc(missing, timeframe, calendar)
                    classification = calendar.classify_interval(missing, missing_end)
                    if classification.open_duration > timedelta(0):
                        reasons.add("MISSING_BAR")
                    if classification.unknown_duration > timedelta(0):
                        reasons.add("CALENDAR_COVERAGE_UNKNOWN")
                    elif classification.open_duration == timedelta(0):
                        reasons.add("KNOWN_MARKET_CLOSURE")
                    missing = missing_end
            elif expected_open is None:
                reasons.add("GAP_SESSION_UNKNOWN")
            else:
                if (timestamp - previous_end) // duration > 100_000:
                    raise ValueError("gap exceeds the supported calendar-check bound")
                missing = previous_end
                while missing < timestamp:
                    if expected_open(missing):
                        reasons.add("MISSING_BAR")
                        break
                    missing += duration
        seen.add(timestamp)
        previous = timestamp
        completion = bar_completion_utc(timestamp, timeframe, calendar)
        if calendar is not None and (
            timestamp < calendar.effective_start_utc
            or completion > calendar.effective_end_utc
            or timestamp < calendar.coverage_start_utc
            or completion > calendar.coverage_end_utc
        ):
            reasons.add("CALENDAR_COVERAGE_UNKNOWN")
        if check_completion and completion > now:
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
        "INVALID_INSTRUMENT",
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
    if reasons & {
        "INCOMPLETE_BAR",
        "GAP_SESSION_UNKNOWN",
        "MISSING_BAR",
        "CALENDAR_COVERAGE_UNKNOWN",
    }:
        return DataQualityReport(DataStatus.INSUFFICIENT_DATA, now, tuple(sorted(reasons)))
    return DataQualityReport(DataStatus.VALID, now, tuple(sorted(reasons)))


def validate_bar_records(
    bars: tuple[MarketBar, ...],
    timeframe: str,
    now: datetime,
    *,
    check_completion: bool = True,
) -> DataQualityReport:
    """Validate provider records without assigning calendar meaning to gaps."""
    return _validate_bars(
        bars,
        timeframe,
        now,
        expected_open=None,
        calendar=None,
        classify_gaps=False,
        check_completion=check_completion,
    )
