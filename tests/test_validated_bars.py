"""Validated historical-bar tests use only deterministic synthetic data."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
from kankalar_gold_ai.core.contracts import DataStatus, MarketBar
from kankalar_gold_ai.data.errors import HistoricalDataQualityError
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.data.validated import ValidatedBarSet
from tests.fixtures.market_data import SYNTHETIC_BARS, SYNTHETIC_SOURCE

NOW = datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)


def _session_open(timestamp: datetime) -> bool:
    return timestamp.weekday() < 5 and 9 <= timestamp.hour < 17


def _bar_at(timestamp: datetime, **changes: object) -> MarketBar:
    bar = MarketBar(
        instrument="TEST_GOLD",
        timeframe="M5",
        timestamp_utc=timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=None,
        source=SYNTHETIC_SOURCE,
    )
    return replace(bar, **changes)


def test_validated_bar_set_preserves_report_and_normalizes_time() -> None:
    offset = timezone(timedelta(hours=2))
    bars = tuple(
        replace(
            bar,
            timestamp_utc=bar.timestamp_utc.astimezone(offset),
        )
        for bar in SYNTHETIC_BARS
    )

    result = ValidatedBarSet.from_bars(bars, "M5", NOW.astimezone(offset))

    assert result.instrument == "TEST_GOLD"
    assert result.timeframe == "M5"
    assert result.source == SYNTHETIC_SOURCE
    assert result.bars == SYNTHETIC_BARS
    assert result.report.status is DataStatus.VALID
    assert result.report.reasons == ()
    assert result.validated_at_utc == NOW
    assert result.expected_open is None


def test_validated_bar_set_rejects_empty_and_inconsistent_sets() -> None:
    with pytest.raises(HistoricalDataQualityError) as empty_error:
        ValidatedBarSet.from_bars((), "M5", NOW)
    assert empty_error.value.report.reasons == ("EMPTY_HISTORY",)

    second = SYNTHETIC_BARS[1]
    invalid_sets = (
        (SYNTHETIC_BARS[0], replace(second, instrument="OTHER")),
        (SYNTHETIC_BARS[0], replace(second, source="other-source")),
        (SYNTHETIC_BARS[0], replace(second, timeframe="M1")),
        (SYNTHETIC_BARS[0], SYNTHETIC_BARS[0]),
        (SYNTHETIC_BARS[1], SYNTHETIC_BARS[0]),
    )
    expected_reasons = (
        "INSTRUMENT_MISMATCH",
        "SOURCE_MISMATCH",
        "TIMEFRAME_MISMATCH",
        "DUPLICATE_BAR",
        "OUT_OF_ORDER",
    )
    for bars, reason in zip(invalid_sets, expected_reasons, strict=True):
        with pytest.raises(HistoricalDataQualityError) as error:
            ValidatedBarSet.from_bars(bars, "M5", NOW)
        assert reason in error.value.report.reasons


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"open": float("nan")}, "INVALID_PRICE"),
        ({"volume": -1.0}, "INVALID_VOLUME"),
        ({"volume": True}, "INVALID_VOLUME"),
        ({"volume": float("inf")}, "INVALID_VOLUME"),
        ({"tick_volume": True}, "INVALID_TICK_VOLUME"),
        ({"real_volume": -1}, "INVALID_REAL_VOLUME"),
        ({"spread_points": 1.5}, "INVALID_SPREAD_POINTS"),
        ({"spread_points": -1}, "INVALID_SPREAD_POINTS"),
    ),
)
def test_validated_bar_set_rejects_invalid_numeric_fields(
    changes: dict[str, object], reason: str
) -> None:
    bars = (replace(SYNTHETIC_BARS[0], **changes),)

    with pytest.raises(HistoricalDataQualityError) as error:
        ValidatedBarSet.from_bars(bars, "M5", NOW)

    assert error.value.report.status is DataStatus.ERROR
    assert reason in error.value.report.reasons


def test_non_valid_quality_report_is_preserved() -> None:
    bars = (SYNTHETIC_BARS[0], SYNTHETIC_BARS[2])

    with pytest.raises(HistoricalDataQualityError) as error:
        ValidatedBarSet.from_bars(bars, "M5", NOW)

    assert error.value.report.status is DataStatus.INSUFFICIENT_DATA
    assert error.value.report.reasons == ("GAP_SESSION_UNKNOWN",)


def test_session_policy_distinguishes_open_gaps_from_known_closures() -> None:
    monday = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    open_gap = (_bar_at(monday), _bar_at(monday + timedelta(minutes=10)))

    with pytest.raises(HistoricalDataQualityError) as unknown_error:
        ValidatedBarSet.from_bars(open_gap, "M5", monday + timedelta(hours=1))
    assert unknown_error.value.report.reasons == ("GAP_SESSION_UNKNOWN",)

    with pytest.raises(HistoricalDataQualityError) as missing_error:
        ValidatedBarSet.from_bars(
            open_gap,
            "M5",
            monday + timedelta(hours=1),
            expected_open=_session_open,
        )
    assert missing_error.value.report.reasons == ("MISSING_BAR",)

    friday = datetime(2026, 1, 2, 16, 55, tzinfo=timezone.utc)
    weekend_gap = (_bar_at(friday), _bar_at(monday))
    with pytest.raises(HistoricalDataQualityError) as closure_unknown_error:
        ValidatedBarSet.from_bars(weekend_gap, "M5", monday + timedelta(hours=1))
    assert closure_unknown_error.value.report.reasons == ("GAP_SESSION_UNKNOWN",)

    known_closure = ValidatedBarSet.from_bars(
        weekend_gap,
        "M5",
        monday + timedelta(hours=1),
        expected_open=_session_open,
    )
    assert known_closure.report.status is DataStatus.VALID
    assert known_closure.expected_open is not None
    assert known_closure.expected_open(datetime(2026, 1, 3, 12, tzinfo=timezone.utc)) is False


def test_session_policy_receives_utc_normalized_timestamps() -> None:
    monday = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    offset = timezone(timedelta(hours=2))
    bars = (
        _bar_at(monday.astimezone(offset)),
        _bar_at((monday + timedelta(minutes=10)).astimezone(offset)),
    )
    observed: list[datetime] = []

    def bounded_session(timestamp: datetime) -> bool:
        observed.append(timestamp)
        return monday <= timestamp < monday + timedelta(hours=1)

    with pytest.raises(HistoricalDataQualityError) as error:
        ValidatedBarSet.from_bars(
            bars,
            "M5",
            monday + timedelta(hours=1),
            expected_open=bounded_session,
        )

    assert error.value.report.reasons == ("MISSING_BAR",)
    assert observed
    assert all(timestamp.tzinfo is timezone.utc for timestamp in observed)


def test_pipeline_additive_method_returns_validated_set_without_extra_provider_access() -> None:
    class CountingProvider:
        def __init__(self) -> None:
            self.calls = 0

        def get_bars(
            self,
            instrument: str,
            timeframe: str,
            start_utc: datetime,
            end_utc: datetime,
        ) -> tuple[MarketBar, ...]:
            self.calls += 1
            return SYNTHETIC_BARS

    provider = CountingProvider()
    pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)
    validated = pipeline.retrieve_validated_bars("TEST_GOLD", "M5", NOW, history_days=1)

    assert isinstance(validated, ValidatedBarSet)
    assert validated.report.status is DataStatus.VALID
    assert validated.instrument == "TEST_GOLD"
    assert provider.calls == 1


def test_pipeline_passes_session_policy_and_rejects_expected_open_gap() -> None:
    monday = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    bars = (_bar_at(monday), _bar_at(monday + timedelta(minutes=10)))
    pipeline = HistoricalDataPipeline(
        MockMarketDataProvider(bars),
        clock=lambda: monday + timedelta(hours=1),
    )

    def bounded_session(timestamp: datetime) -> bool:
        return monday <= timestamp < monday + timedelta(hours=1)

    with pytest.raises(HistoricalDataQualityError) as error:
        pipeline.retrieve_validated_bars(
            "TEST_GOLD",
            "M5",
            monday + timedelta(hours=1),
            history_days=1,
            expected_open=bounded_session,
        )

    assert error.value.report.status is DataStatus.INSUFFICIENT_DATA
    assert error.value.report.reasons == ("MISSING_BAR",)


def test_existing_retrieval_still_returns_tuple_and_mock_needs_no_network() -> None:
    pipeline = HistoricalDataPipeline(MockMarketDataProvider(SYNTHETIC_BARS), clock=lambda: NOW)

    bars = pipeline.retrieve_bars("TEST_GOLD", "M5", NOW, history_days=1)

    assert isinstance(bars, tuple)
    assert bars == SYNTHETIC_BARS
