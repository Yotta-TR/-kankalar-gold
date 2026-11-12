"""In-memory provider for explicitly synthetic test fixtures."""

from datetime import datetime

from kankalar_gold_ai.core.contracts import MarketBar, as_utc


class MockMarketDataProvider:
    """Read-only provider that filters supplied bars and never generates data."""

    def __init__(self, bars: tuple[MarketBar, ...]) -> None:
        self._bars = bars

    def get_bars(
        self,
        instrument: str,
        timeframe: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[MarketBar, ...]:
        start = as_utc(start_utc)
        end = as_utc(end_utc)
        if start > end:
            raise ValueError("start_utc must not be after end_utc")
        return tuple(
            bar
            for bar in self._bars
            if bar.instrument == instrument
            and bar.timeframe == timeframe
            and start <= bar.timestamp_utc <= end
        )
