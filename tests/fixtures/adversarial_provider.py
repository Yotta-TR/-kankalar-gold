"""Test-only provider that ignores the requested range (adversarial, synthetic, offline)."""

from __future__ import annotations

from datetime import datetime

from kankalar_gold_ai.core.contracts import MarketBar


class OverreachingProvider:
    """Returns a fixed tuple regardless of the requested range and records every request."""

    def __init__(self, bars: tuple[MarketBar, ...]) -> None:
        self._bars = bars
        self.requests: list[tuple[str, str, datetime, datetime]] = []

    def get_bars(
        self,
        instrument: str,
        timeframe: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[MarketBar, ...]:
        self.requests.append((instrument, timeframe, start_utc, end_utc))
        return self._bars
