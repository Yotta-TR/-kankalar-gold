"""Small synthetic fixture set; prices are invented solely for unit tests."""

from datetime import datetime, timedelta, timezone

from kankalar_gold_ai.core.contracts import MarketBar

SYNTHETIC_SOURCE = "synthetic-test-fixture"
SYNTHETIC_BARS = tuple(
    MarketBar(
        instrument="TEST_GOLD",
        timeframe="M5",
        timestamp_utc=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * index),
        open=100.0 + index,
        high=101.0 + index,
        low=99.0 + index,
        close=100.5 + index,
        volume=None,
        source=SYNTHETIC_SOURCE,
    )
    for index in range(3)
)
