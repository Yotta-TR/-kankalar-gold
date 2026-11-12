"""Historical data pipeline: bounded retrieval, UTC normalization, completeness checks."""

from datetime import datetime, timedelta, timezone
from typing import Callable

from kankalar_gold_ai.core.contracts import (
    DataQualityReport,
    DataStatus,
    MarketBar,
    MarketDataProvider,
    as_utc,
)
from kankalar_gold_ai.data.errors import HistoricalDataQualityError
from kankalar_gold_ai.data.quality import TIMEFRAME_DURATIONS, validate_bars
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

    def retrieve_bars(
        self,
        instrument: str,
        timeframe: str,
        end_utc: datetime,
        history_days: int,
        expected_open: Callable[[datetime], bool] | None = None,
    ) -> tuple[MarketBar, ...]:
        """
        Retrieve historical bars from end_utc backwards for history_days.

        Args:
            instrument: symbol name (e.g., 'XAUUSD')
            timeframe: one of M1, M5, M15, H1, H4, D1
            end_utc: end of the range (inclusive)
            history_days: number of trading/calendar days to retrieve
            expected_open: optional callable to check if a session was open at a given time

        Returns:
            tuple of MarketBar objects, sorted by timestamp ascending

        Raises:
            ValueError: if arguments are invalid or retrieved bars fail validation
        """
        bars, now = self._retrieve_bars(instrument, timeframe, end_utc, history_days)
        report = validate_bars(bars, timeframe, now, expected_open=expected_open)
        self._require_valid(report)
        return bars

    def retrieve_validated_bars(
        self,
        instrument: str,
        timeframe: str,
        end_utc: datetime,
        history_days: int,
        expected_open: Callable[[datetime], bool] | None = None,
    ) -> ValidatedBarSet:
        """Retrieve bars through the existing provider path and return a validated carrier."""
        bars, now = self._retrieve_bars(instrument, timeframe, end_utc, history_days)
        return ValidatedBarSet.from_bars(
            bars,
            timeframe,
            now,
            expected_open=expected_open,
            expected_instrument=instrument,
        )

    def _retrieve_bars(
        self,
        instrument: str,
        timeframe: str,
        end_utc: datetime,
        history_days: int,
    ) -> tuple[tuple[MarketBar, ...], datetime]:
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
        return self._provider.get_bars(instrument, timeframe, start, end), now

    def latest_complete_bar(
        self,
        instrument: str,
        timeframe: str,
    ) -> MarketBar | None:
        """
        Retrieve the most recent complete bar for the given instrument and timeframe.

        Returns None if no complete bars are available.
        """
        if timeframe not in self.SUPPORTED_TIMEFRAMES:
            raise ValueError(f"unsupported timeframe: {timeframe}")

        now = self._now()
        duration = TIMEFRAME_DURATIONS[timeframe]

        # The most recent complete bar ended at most duration ago
        # Calculate using timestamps (seconds) for proper arithmetic
        now_ts = now.timestamp()
        duration_seconds = duration.total_seconds()
        bar_end_ts = (now_ts // duration_seconds) * duration_seconds
        bar_end = datetime.fromtimestamp(bar_end_ts, tz=timezone.utc)

        if bar_end > now - timedelta(seconds=1):
            # No complete bar available yet; we're still in the current bar
            return None

        start = bar_end - duration
        bars = self._provider.get_bars(instrument, timeframe, start, bar_end)

        if not bars:
            return None

        report = validate_bars(bars, timeframe, now)
        self._require_valid(report)

        if len(bars) > 1:
            raise ValueError("Expected at most one bar in the range")

        return bars[0]

    @staticmethod
    def _require_valid(report: DataQualityReport) -> None:
        if report.status is not DataStatus.VALID:
            raise HistoricalDataQualityError(report)

    def validate_bars(
        self,
        bars: tuple[MarketBar, ...],
        timeframe: str,
        expected_open: Callable[[datetime], bool] | None = None,
    ) -> DataQualityReport:
        """
        Validate a set of bars for completeness, ordering, and OHLC integrity.

        Does not retrieve data; purely validates supplied bars.
        """
        return validate_bars(bars, timeframe, self._now(), expected_open=expected_open)

    def check_coverage(
        self,
        instrument: str,
        timeframe: str,
        start_utc: datetime,
        end_utc: datetime,
        expected_open: Callable[[datetime], bool] | None = None,
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
        return validate_bars(bars, timeframe, self._now(), expected_open=expected_open)

    def __repr__(self) -> str:
        return f"HistoricalDataPipeline(provider={self._provider!r})"
