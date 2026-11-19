"""Historical data pipeline: bounded retrieval, UTC normalization, completeness checks."""

from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from kankalar_gold_ai.core.contracts import (
    DataQualityReport,
    DataStatus,
    MarketBar,
    MarketDataProvider,
    as_utc,
)
from kankalar_gold_ai.data.calendar import SessionCalendar, validate_calendar_scope
from kankalar_gold_ai.data.errors import EmptyHistoricalDataError, HistoricalDataQualityError
from kankalar_gold_ai.data.quality import (
    TIMEFRAME_DURATIONS,
    bar_completion_utc,
    validate_bars,
)
from kankalar_gold_ai.data.validated import ValidatedBarSet


class HistoricalDataPipeline:
    """Retrieve, normalize, validate, and quality-check historical bars."""

    SUPPORTED_TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1")
    MAX_HISTORY_DAYS = 366

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return as_utc(self._clock())

    def checked_at_utc(self) -> datetime:
        """Return the pipeline clock value used to timestamp quality outcomes."""
        return self._now()

    def retrieve_bars(
        self,
        instrument: str,
        timeframe: str,
        end_utc: datetime,
        history_days: int,
        expected_open: Callable[[datetime], bool] | None = None,
        *,
        calendar: SessionCalendar | None = None,
    ) -> tuple[MarketBar, ...]:
        """
        Retrieve historical bars from end_utc backwards for history_days.

        Args:
            instrument: symbol name (e.g., 'XAUUSD')
            timeframe: one of M1, M5, M15, H1, H4, D1
            end_utc: inclusive bar-completion cutoff
            history_days: number of trading/calendar days to retrieve
            expected_open: optional callable to check if a session was open at a given time

        Returns:
            tuple of MarketBar objects, sorted by timestamp ascending. This raw
            tuple carries no cutoff provenance; use retrieve_validated_bars for studies.

        Raises:
            ValueError: if arguments are invalid or retrieved bars fail validation
        """
        bars, now, _cutoff = self._retrieve_bars(
            instrument, timeframe, end_utc, history_days, calendar=calendar
        )
        report = validate_bars(
            bars,
            timeframe,
            now,
            expected_open=expected_open,
            calendar=calendar,
            expected_instrument=instrument,
        )
        self._require_valid(report)
        return bars

    def retrieve_validated_bars(
        self,
        instrument: str,
        timeframe: str,
        end_utc: datetime,
        history_days: int,
        expected_open: Callable[[datetime], bool] | None = None,
        *,
        calendar: SessionCalendar | None = None,
    ) -> ValidatedBarSet:
        """Retrieve bars completed by end_utc and bind them to that cutoff."""
        bars, now, cutoff = self._retrieve_bars(
            instrument, timeframe, end_utc, history_days, calendar=calendar
        )
        return ValidatedBarSet.from_bars(
            bars,
            timeframe,
            now,
            cutoff_utc=cutoff,
            expected_open=expected_open,
            expected_instrument=instrument,
            calendar=calendar,
        )

    def _retrieve_bars(
        self,
        instrument: str,
        timeframe: str,
        end_utc: datetime,
        history_days: int,
        *,
        calendar: SessionCalendar | None = None,
    ) -> tuple[tuple[MarketBar, ...], datetime, datetime]:
        if timeframe not in self.SUPPORTED_TIMEFRAMES:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        if history_days < 1 or history_days > self.MAX_HISTORY_DAYS:
            raise ValueError(
                f"history_days must be between 1 and {self.MAX_HISTORY_DAYS}, got {history_days}"
            )
        end = as_utc(end_utc)
        now = self._now()
        if end > now:
            raise ValueError(f"end_utc ({end}) must not be in the future (now={now})")
        start = end - timedelta(days=history_days)
        duration = TIMEFRAME_DURATIONS[timeframe]
        calendar_d1 = timeframe == "D1" and calendar is not None
        provider_end = end if calendar_d1 else end - duration
        retrieved = self._provider.get_bars(instrument, timeframe, start, provider_end)
        bars = tuple(
            bar
            for bar in retrieved
            if not (calendar_d1 and isinstance(bar, MarketBar) and bar.timestamp_utc == end)
        )
        for bar in bars:
            if not isinstance(bar, MarketBar):
                raise TypeError("provider must return MarketBar instances")
            if not start <= bar.timestamp_utc <= provider_end:
                raise ValueError("provider returned a bar outside the requested open-time range")
            if calendar_d1 and bar.timestamp_utc >= end:
                raise ValueError("calendar D1 retrieval uses a half-open [start, cutoff) interval")
            if bar_completion_utc(bar.timestamp_utc, timeframe, calendar) > end:
                raise ValueError("provider returned a bar completing after the cutoff")
        return bars, now, end

    def latest_complete_bar(
        self,
        instrument: str,
        timeframe: str,
        *,
        calendar: SessionCalendar | None = None,
        lookback_days: int = MAX_HISTORY_DAYS,
    ) -> MarketBar | None:
        """
        Retrieve the most recent complete bar for the given instrument and timeframe.

        For calendar-scoped D1, search the preceding ``lookback_days`` UTC
        days, bounded by MAX_HISTORY_DAYS. If no completed bars are available,
        returns None only for a fully known closed search interval. Other
        timeframes retain their existing single-interval lookup.
        """
        if timeframe not in self.SUPPORTED_TIMEFRAMES:
            raise ValueError(f"unsupported timeframe: {timeframe}")

        now = self._now()
        duration = TIMEFRAME_DURATIONS[timeframe]

        if timeframe == "D1" and calendar is not None:
            if type(lookback_days) is not int or not 1 <= lookback_days <= self.MAX_HISTORY_DAYS:
                raise ValueError(f"lookback_days must be between 1 and {self.MAX_HISTORY_DAYS}")
            validate_calendar_scope(calendar, (), expected_instrument=instrument)
            local_day = now.astimezone(ZoneInfo(calendar.timezone_name)).date()
            cutoff = calendar.local_day_start_utc(local_day)
            start = now - timedelta(days=lookback_days)
            if start >= cutoff:
                raise ValueError("lookback_days does not reach a completed local calendar day")
            try:
                retrieved = self._provider.get_bars(instrument, timeframe, start, cutoff)
            except EmptyHistoricalDataError:
                retrieved = ()
            if any(not isinstance(bar, MarketBar) for bar in retrieved):
                raise TypeError("provider must return MarketBar instances")
            validate_calendar_scope(calendar, retrieved, expected_instrument=instrument)
            for bar in retrieved:
                if not start <= bar.timestamp_utc <= cutoff:
                    raise ValueError(
                        "provider returned a bar outside the requested open-time range"
                    )
            bars = tuple(
                bar
                for bar in retrieved
                if bar.timestamp_utc < cutoff
                and bar_completion_utc(bar.timestamp_utc, timeframe, calendar) <= now
            )
            if not bars:
                self._require_closed_interval(calendar, start, cutoff, now)
                return None
            report = validate_bars(
                bars, timeframe, now, calendar=calendar, expected_instrument=instrument
            )
            self._require_valid(report)
            last_completion = bar_completion_utc(bars[-1].timestamp_utc, timeframe, calendar)
            if last_completion < cutoff:
                self._require_closed_interval(calendar, last_completion, cutoff, now)
            return bars[-1]

        # The most recent complete bar ended at most duration ago
        # Calculate using timestamps (seconds) for proper arithmetic
        now_ts = now.timestamp()
        duration_seconds = duration.total_seconds()
        bar_end_ts = (now_ts // duration_seconds) * duration_seconds
        bar_end = datetime.fromtimestamp(bar_end_ts, tz=timezone.utc)
        start = bar_end - duration

        if bar_end > now - timedelta(seconds=1):
            # No complete bar available yet; we're still in the current bar
            return None

        bars = self._provider.get_bars(instrument, timeframe, start, bar_end)

        if not bars:
            return None

        report = validate_bars(
            bars, timeframe, now, calendar=calendar, expected_instrument=instrument
        )
        self._require_valid(report)

        if len(bars) > 1:
            raise ValueError("Expected at most one bar in the range")

        return bars[0]

    @staticmethod
    def _require_valid(report: DataQualityReport) -> None:
        if report.status is not DataStatus.VALID:
            raise HistoricalDataQualityError(report)

    @staticmethod
    def _require_closed_interval(
        calendar: SessionCalendar, start: datetime, end: datetime, now: datetime
    ) -> None:
        gap = calendar.classify_interval(start, end)
        reasons = []
        if gap.open_duration > timedelta(0):
            reasons.append("MISSING_BAR")
        if gap.unknown_duration > timedelta(0):
            reasons.append("CALENDAR_COVERAGE_UNKNOWN")
        if reasons:
            raise HistoricalDataQualityError(
                DataQualityReport(DataStatus.INSUFFICIENT_DATA, now, tuple(reasons))
            )

    def validate_bars(
        self,
        bars: tuple[MarketBar, ...],
        timeframe: str,
        expected_open: Callable[[datetime], bool] | None = None,
        *,
        calendar: SessionCalendar | None = None,
        expected_instrument: str | None = None,
    ) -> DataQualityReport:
        """
        Validate a set of bars for completeness, ordering, and OHLC integrity.

        Does not retrieve data; purely validates supplied bars.
        """
        return validate_bars(
            bars,
            timeframe,
            self._now(),
            expected_open=expected_open,
            calendar=calendar,
            expected_instrument=expected_instrument,
        )

    def check_coverage(
        self,
        instrument: str,
        timeframe: str,
        start_utc: datetime,
        end_utc: datetime,
        expected_open: Callable[[datetime], bool] | None = None,
        *,
        calendar: SessionCalendar | None = None,
    ) -> DataQualityReport:
        """
        Retrieve and validate bars for a specific time range.

        Useful for checking if a requested period has complete historical coverage.
        """
        if timeframe not in self.SUPPORTED_TIMEFRAMES:
            raise ValueError(f"unsupported timeframe: {timeframe}")

        start = as_utc(start_utc)
        end = as_utc(end_utc)

        if start > end:
            raise ValueError("start_utc must not be after end_utc")

        if end > self._now():
            raise ValueError("end_utc must not be in the future")

        bars = self._provider.get_bars(instrument, timeframe, start, end)
        return validate_bars(
            bars,
            timeframe,
            self._now(),
            expected_open=expected_open,
            calendar=calendar,
            expected_instrument=instrument,
        )

    def __repr__(self) -> str:
        return f"HistoricalDataPipeline(provider={self._provider!r})"
