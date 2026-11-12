"""Quality checks use synthetic data only and no external connections."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kankalar_gold_ai.core.contracts import DataStatus, MarketQuote
from kankalar_gold_ai.data.quality import validate_bars, validate_quote
from tests.fixtures.market_data import SYNTHETIC_BARS

NOW = datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)


def quote(**changes: object) -> MarketQuote:
    original = MarketQuote(
        instrument="TEST_GOLD",
        timestamp_utc=NOW,
        bid=100,
        ask=100.1,
        last=None,
        timestamp_milliseconds=None,
        source="synthetic",
        retrieved_at_utc=NOW,
    )
    return replace(original, **changes)


def test_quote_quality_reports_stale_crossed_and_abnormal_spread() -> None:
    assert validate_quote(quote(), NOW, timedelta(seconds=30), 1).status == DataStatus.VALID
    assert (
        validate_quote(
            quote(timestamp_utc=NOW - timedelta(seconds=31)), NOW, timedelta(seconds=30), 1
        ).status
        == DataStatus.STALE
    )
    assert "INVALID_BID_ASK" in validate_quote(quote(ask=99), NOW, timedelta(seconds=30), 1).reasons
    assert (
        "ABNORMAL_SPREAD" in validate_quote(quote(ask=102), NOW, timedelta(seconds=30), 1).reasons
    )
    assert (
        "INCONSISTENT_TICK_TIMESTAMP"
        in validate_quote(quote(timestamp_milliseconds=0), NOW, timedelta(seconds=30), 1).reasons
    )


def test_bar_quality_flags_duplicates_reordering_incomplete_and_nan() -> None:
    assert validate_bars(SYNTHETIC_BARS, "M5", NOW).status == DataStatus.VALID
    report = validate_bars(
        (SYNTHETIC_BARS[1], SYNTHETIC_BARS[0], SYNTHETIC_BARS[0]),
        "M5",
        NOW,
    )
    assert {"DUPLICATE_BAR", "OUT_OF_ORDER"} <= set(report.reasons)
    assert report.status == DataStatus.ERROR
    assert (
        "INCOMPLETE_BAR" in validate_bars(SYNTHETIC_BARS, "M5", NOW - timedelta(seconds=1)).reasons
    )
    assert (
        "INVALID_PRICE"
        in validate_bars((replace(SYNTHETIC_BARS[0], open=float("nan")),), "M5", NOW).reasons
    )


def test_gaps_are_not_assumed_open_without_a_session_calendar() -> None:
    bars = (SYNTHETIC_BARS[0], SYNTHETIC_BARS[2])
    assert validate_bars(bars, "M5", NOW).reasons == ("GAP_SESSION_UNKNOWN",)

    def weekday_day_session(timestamp: datetime) -> bool:
        return timestamp.weekday() < 5 and 9 <= timestamp.hour < 17

    monday_now = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    open_session_bars = tuple(
        replace(
            bar,
            timestamp_utc=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
            + index * 2 * timedelta(minutes=5),
        )
        for index, bar in enumerate((SYNTHETIC_BARS[0], SYNTHETIC_BARS[1]))
    )
    assert validate_bars(
        open_session_bars, "M5", monday_now, expected_open=weekday_day_session
    ).reasons == ("MISSING_BAR",)
    assert (
        validate_bars(bars, "M5", NOW, expected_open=weekday_day_session).status == DataStatus.VALID
    )


def test_quote_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        quote(timestamp_utc=datetime(2026, 1, 1))
