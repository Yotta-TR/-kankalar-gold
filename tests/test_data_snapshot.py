"""Canonical bar-snapshot serialization tests use synthetic values only."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kankalar_gold_ai.core.contracts import MarketBar
from kankalar_gold_ai.data.snapshot import (
    canonical_bar_snapshot_json,
    compute_bar_snapshot_id,
)


def snapshot_bar(**changes: object) -> MarketBar:
    bar = MarketBar(
        instrument="GOLD_USD",
        timeframe="M1",
        timestamp_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        open=1,
        high=1.1,
        low=0.9,
        close=1.05,
        volume=None,
        source="synthetic",
        tick_volume=2,
        real_volume=3,
        spread_points=4,
    )
    return replace(bar, **changes)


def test_snapshot_canonical_json_and_pinned_hash() -> None:
    bars = (snapshot_bar(),)

    assert canonical_bar_snapshot_json(bars) == (
        '{"algorithm":"bars-v1","bars":[{"close":1.05,"high":1.1,"low":0.9,'
        '"open":1.0,"real_volume":3,"spread_points":4,"tick_volume":2,'
        '"timestamp_utc":"2026-01-01T00:00:00Z","volume":null}],'
        '"instrument":"GOLD_USD","source":"synthetic","timeframe":"M1"}'
    )
    assert compute_bar_snapshot_id(bars) == (
        "bars-v1:sha256:9f57a65577107f899dd1b4e7d3c658e0968834d44b138a5cb39af5fc76c7ccc4"
    )


def test_identical_data_and_equivalent_utc_offsets_have_identical_ids() -> None:
    bar = snapshot_bar()
    offset_bar = replace(
        bar,
        timestamp_utc=datetime(2026, 1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    )

    assert compute_bar_snapshot_id((bar,)) == compute_bar_snapshot_id((snapshot_bar(),))
    assert compute_bar_snapshot_id((bar,)) == compute_bar_snapshot_id((offset_bar,))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("timestamp_utc", datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)),
        ("instrument", "OTHER_GOLD"),
        ("timeframe", "M5"),
        ("source", "other-source"),
        ("open", 1.01),
        ("high", 1.11),
        ("low", 0.89),
        ("close", 1.04),
        ("volume", 1.0),
        ("tick_volume", 5),
        ("real_volume", 6),
        ("spread_points", 7),
    ),
)
def test_each_relevant_market_field_changes_snapshot_id(field: str, value: object) -> None:
    original = snapshot_bar()
    changed = replace(original, **{field: value})

    assert compute_bar_snapshot_id((original,)) != compute_bar_snapshot_id((changed,))


def test_retrieval_time_is_excluded_and_none_differs_from_zero() -> None:
    original = snapshot_bar()
    retrieved_later = replace(
        original,
        retrieved_at_utc=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    zero_volume = replace(original, volume=0.0)

    assert compute_bar_snapshot_id((original,)) == compute_bar_snapshot_id((retrieved_later,))
    assert compute_bar_snapshot_id((original,)) != compute_bar_snapshot_id((zero_volume,))


@pytest.mark.parametrize("field", ("tick_volume", "real_volume", "spread_points"))
def test_optional_integer_none_differs_from_zero(field: str) -> None:
    original = snapshot_bar(**{field: None})
    zero = snapshot_bar(**{field: 0})

    assert compute_bar_snapshot_id((original,)) != compute_bar_snapshot_id((zero,))


def test_numeric_normalization_and_non_finite_rejection() -> None:
    integer_values = snapshot_bar(open=1)
    float_values = snapshot_bar(open=1.0)
    negative_zero = snapshot_bar(volume=-0.0)
    positive_zero = snapshot_bar(volume=0.0)

    assert compute_bar_snapshot_id((integer_values,)) == compute_bar_snapshot_id((float_values,))
    assert compute_bar_snapshot_id((negative_zero,)) != compute_bar_snapshot_id((positive_zero,))
    with pytest.raises(ValueError):
        compute_bar_snapshot_id((replace(snapshot_bar(), close=float("nan")),))
    with pytest.raises(ValueError):
        compute_bar_snapshot_id((replace(snapshot_bar(), volume=float("inf")),))
    with pytest.raises(ValueError):
        compute_bar_snapshot_id((replace(snapshot_bar(), tick_volume=True),))
    with pytest.raises(ValueError):
        compute_bar_snapshot_id((replace(snapshot_bar(), open=True, low=0.5),))


def test_snapshot_rejects_empty_mixed_duplicate_and_unordered_bars() -> None:
    first = snapshot_bar()
    second = replace(first, timestamp_utc=first.timestamp_utc + timedelta(minutes=1))
    with pytest.raises(ValueError, match="must not be empty"):
        compute_bar_snapshot_id(())
    with pytest.raises(ValueError, match="SOURCE_MISMATCH"):
        compute_bar_snapshot_id((first, replace(second, source="other")))
    with pytest.raises(ValueError, match="INSTRUMENT_MISMATCH"):
        compute_bar_snapshot_id((first, replace(second, instrument="OTHER")))
    with pytest.raises(ValueError, match="TIMEFRAME_MISMATCH"):
        compute_bar_snapshot_id((first, replace(second, timeframe="M5")))
    with pytest.raises(ValueError, match="DUPLICATE_BAR"):
        compute_bar_snapshot_id((first, first))
    with pytest.raises(ValueError, match="OUT_OF_ORDER"):
        compute_bar_snapshot_id((second, first))
