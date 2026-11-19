"""Synthetic, deterministic tests for multi-timeframe execution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from kankalar_gold_ai.core.contracts import (
    DataQualityReport,
    DataStatus,
    Direction,
    MarketBar,
    StudyResult,
    StudyStatus,
)
from kankalar_gold_ai.core.study_results import StudyResultKey
from kankalar_gold_ai.data.calendar import CalendarProvenance, SessionCalendar, WeeklySession
from kankalar_gold_ai.data.quality import TIMEFRAME_DURATIONS
from kankalar_gold_ai.data.snapshot import compute_bar_snapshot_id
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import SQLitePersistence
from kankalar_gold_ai.studies import (
    AnalysisCompleteness,
    CalendarAwareFreshnessPolicy,
    DataFailure,
    FreshnessPolicy,
    MultiTimeframeAnalysisCoordinator,
    MultiTimeframeAnalysisRequest,
    PreflightEligibility,
    StudyExecutionSpec,
    StudyOutput,
    StudyPersistenceError,
    StudyRegistry,
    StudyRequestError,
    StudyRunner,
)
from kankalar_gold_ai.studies.base import ConfigValue
from kankalar_gold_ai.studies.multitimeframe import SUPPORTED_TIMEFRAMES

CUTOFF = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
INSTRUMENT = "SYNTHETIC_GOLD"
SOURCE = "synthetic-coordinator"


class SyntheticStudy:
    version = "1.0.0"
    required_timeframes = frozenset(SUPPORTED_TIMEFRAMES)

    def __init__(self, study_id: str, *, minimum: int = 2, fail: bool = False) -> None:
        self.study_id = study_id
        self.minimum = minimum
        self.fail = fail
        self.calls: list[tuple[MarketBar, ...]] = []

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        return dict(raw)

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        return self.minimum

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        self.calls.append(bars)
        if self.fail:
            raise RuntimeError("synthetic computation failure")
        return StudyOutput(status=StudyStatus.AVAILABLE, value=bars[-1].close)


class CountingStore:
    def __init__(self, path: Path, *, fail_save: int | None = None) -> None:
        self.inner = SQLitePersistence(path)
        self.inner.initialize()
        self.lookups: list[StudyResultKey] = []
        self.saved: list[StudyResult] = []
        self.save_attempts = 0
        self.fail_save = fail_save

    def find_cached_study_result(self, key: StudyResultKey) -> StudyResult | None:
        self.lookups.append(key)
        return self.inner.find_cached_study_result(key)

    def save_study_result(self, result: StudyResult) -> None:
        self.save_attempts += 1
        if self.save_attempts == self.fail_save:
            raise OSError("synthetic persistence failure")
        self.inner.save_study_result(result)
        self.saved.append(result)


def bars(
    timeframe: str,
    *,
    count: int = 3,
    age: timedelta = timedelta(),
    calendar: SessionCalendar | None = None,
) -> ValidatedBarSet:
    duration = TIMEFRAME_DURATIONS[timeframe]
    items = tuple(
        MarketBar(
            instrument=INSTRUMENT,
            timeframe=timeframe,
            timestamp_utc=CUTOFF - age - duration * (count - index),
            open=100.0 + index,
            high=102.0 + index,
            low=99.0 + index,
            close=101.0 + index,
            volume=None,
            source=SOURCE,
        )
        for index in range(count)
    )
    return ValidatedBarSet.from_bars(items, timeframe, CUTOFF, cutoff_utc=CUTOFF, calendar=calendar)


def spec(timeframe: str, study_id: str = "trend.first") -> StudyExecutionSpec:
    return StudyExecutionSpec(study_id, "1.0.0", timeframe)


def request(
    executions: tuple[StudyExecutionSpec, ...],
    inputs: Mapping[str, ValidatedBarSet | DataFailure],
) -> MultiTimeframeAnalysisRequest:
    return MultiTimeframeAnalysisRequest(
        instrument=INSTRUMENT,
        as_of_utc=CUTOFF,
        executions=executions,
        inputs_by_timeframe=inputs,
        freshness_policy=FreshnessPolicy.one_timeframe_duration(),
    )


def setup(
    tmp_path: Path, studies: tuple[SyntheticStudy, ...], *, fail_save: int | None = None
) -> tuple[MultiTimeframeAnalysisCoordinator, CountingStore]:
    registry = StudyRegistry()
    for study in studies:
        registry.register(study)
    store = CountingStore(tmp_path / "coordinator.db", fail_save=fail_save)
    return MultiTimeframeAnalysisCoordinator(StudyRunner(registry, store)), store


@pytest.mark.parametrize("timeframes", [SUPPORTED_TIMEFRAMES, ("M5", "H4")])
def test_timeframes_cache_snapshot_identity_and_cutoff(
    tmp_path: Path, timeframes: tuple[str, ...]
) -> None:
    study = SyntheticStudy("trend.first")
    coordinator, store = setup(tmp_path, (study,))
    inputs = {timeframe: bars(timeframe) for timeframe in timeframes}
    analysis_request = request(tuple(spec(tf) for tf in reversed(timeframes)), inputs)

    first = coordinator.run(analysis_request)
    second = coordinator.run(analysis_request)

    assert first.completeness is AnalysisCompleteness.COMPLETE
    assert tuple(item.result.timeframe for item in first.components) == timeframes
    assert len(study.calls) == len(timeframes)
    assert store.save_attempts == len(timeframes)
    assert len(store.lookups) == 2 * len(timeframes)
    assert store.lookups[: len(timeframes)] == store.lookups[len(timeframes) :]
    assert len(set(store.lookups[: len(timeframes)])) == len(timeframes)
    assert tuple(item.timeframe for item in store.saved) == timeframes
    assert tuple(seen[0].timeframe for seen in study.calls) == timeframes
    assert first == second
    assert first.result_identity == second.result_identity
    assert first.result_manifest_json == second.result_manifest_json
    for item in first.components:
        timeframe = item.result.timeframe
        assert item.result.as_of_utc == CUTOFF
        assert item.result.data_snapshot_id == compute_bar_snapshot_id(inputs[timeframe].bars)
        assert item.result.status is StudyStatus.AVAILABLE
    for seen in study.calls:
        assert all(bar.timestamp_utc + TIMEFRAME_DURATIONS[bar.timeframe] <= CUTOFF for bar in seen)


def test_multiple_studies_share_one_timeframe_input(tmp_path: Path) -> None:
    first_study = SyntheticStudy("trend.first")
    second_study = SyntheticStudy("trend.second")
    coordinator, store = setup(tmp_path, (first_study, second_study))
    input_bars = bars("M5")
    result = coordinator.run(request((spec("M5", "trend.second"), spec("M5")), {"M5": input_bars}))

    assert [item.result.study_id for item in result.components] == ["trend.first", "trend.second"]
    assert [item.result.data_snapshot_id for item in result.components] == [
        compute_bar_snapshot_id(input_bars.bars)
    ] * 2
    assert first_study.calls == second_study.calls == [input_bars.bars]
    assert len(store.saved) == 2
    assert store.lookups[0] != store.lookups[1]


@pytest.mark.parametrize(
    "status",
    [DataStatus.UNAVAILABLE, DataStatus.STALE, DataStatus.INSUFFICIENT_DATA, DataStatus.ERROR],
)
def test_explicit_data_failures_preserve_status_and_reason(
    tmp_path: Path, status: DataStatus
) -> None:
    study = SyntheticStudy("trend.first")
    coordinator, store = setup(tmp_path, (study,))
    checked = CUTOFF + timedelta(seconds=1)
    failure = DataFailure(
        INSTRUMENT, "M5", CUTOFF, DataQualityReport(status, checked, ("SYNTHETIC_FAILURE",))
    )
    result = coordinator.run(request((spec("M5"),), {"M5": failure}))
    item = result.components[0].result

    assert result.completeness is AnalysisCompleteness.NO_AVAILABLE_COMPONENTS
    assert item.status is StudyStatus(status.value)
    assert item.evidence == (f"DATA_{status.value}", "SYNTHETIC_FAILURE")
    assert item.data_snapshot_id is None and item.value is None
    assert item.direction is Direction.NOT_APPLICABLE
    assert dict(item.provenance)["data_checked_at_utc"] == "2026-11-14T12:00:01Z"
    assert not study.calls and not store.lookups and store.saved == [item]


def test_stale_bars_and_inclusive_freshness_boundary(tmp_path: Path) -> None:
    study = SyntheticStudy("trend.first")
    coordinator, store = setup(tmp_path, (study,))
    duration = TIMEFRAME_DURATIONS["M5"]
    stale = bars("M5", count=1, age=duration + timedelta(microseconds=1))
    boundary = bars("H1", age=TIMEFRAME_DURATIONS["H1"])
    result = coordinator.run(request((spec("H1"), spec("M5")), {"M5": stale, "H1": boundary}))

    assert result.completeness is AnalysisCompleteness.PARTIAL
    stale_item, fresh_item = result.components
    assert stale_item.component.eligibility is PreflightEligibility.STALE
    assert stale_item.result.status is StudyStatus.STALE
    assert stale_item.result.data_snapshot_id is None
    assert stale_item.result.value is None and stale_item.result.state is None
    assert stale_item.result.normalized_value is None and stale_item.result.uncertainty is None
    assert stale_item.result.direction is Direction.NOT_APPLICABLE
    assert stale_item.result.evidence == ("DATA_STALE", "FRESHNESS_MAX_AGE_EXCEEDED")
    assert dict(stale_item.result.provenance)["data_checked_at_utc"] == "2026-11-14T12:00:00Z"
    manifest = json.loads(result.preflight.input_manifest_json)
    assert manifest["components"][0]["data_snapshot_id"] == compute_bar_snapshot_id(stale.bars)
    assert manifest["components"][0]["bar_age_microseconds"] == 300_000_001
    assert manifest["freshness_policy"] == {
        "id": "one-timeframe-duration",
        "version": "1.0.0",
        "max_age_microseconds_by_timeframe": {
            "M1": 60_000_000,
            "M5": 300_000_000,
            "M15": 900_000_000,
            "H1": 3_600_000_000,
            "H4": 14_400_000_000,
            "D1": 86_400_000_000,
        },
    }
    assert "session_calendar" not in manifest
    assert fresh_item.result.status is StudyStatus.AVAILABLE
    assert study.calls == [boundary.bars]
    assert len(store.lookups) == 1 and len(store.saved) == 2


def test_unknown_calendar_freshness_fails_before_study_computation(tmp_path: Path) -> None:
    study = SyntheticStudy("trend.first")
    coordinator, store = setup(tmp_path, (study,))
    calendar = SessionCalendar(
        calendar_id="synthetic.coordinator",
        version="calendar-v1",
        provider_id=SOURCE,
        instrument_id=INSTRUMENT,
        provenance=CalendarProvenance(
            source="synthetic-test-fixture",
            reference="tests/test_multitimeframe_coordinator.py",
        ),
        timezone_name="UTC",
        effective_start_utc=CUTOFF - timedelta(days=1),
        effective_end_utc=CUTOFF + timedelta(days=1),
        coverage_start_utc=CUTOFF - timedelta(days=1),
        coverage_end_utc=CUTOFF - timedelta(hours=1),
        weekly_sessions=tuple(WeeklySession(day, time(0), time(23, 59)) for day in range(7)),
    )
    bar_set = bars("M5", age=timedelta(hours=2), calendar=calendar)
    analysis_request = MultiTimeframeAnalysisRequest(
        instrument=INSTRUMENT,
        as_of_utc=CUTOFF,
        executions=(spec("M5"),),
        inputs_by_timeframe={"M5": bar_set},
        freshness_policy=CalendarAwareFreshnessPolicy(
            max_wall_clock_age=timedelta(hours=3),
            max_open_market_age=timedelta(hours=3),
        ),
        session_calendar=calendar,
    )

    result = coordinator.run(analysis_request)

    assert result.components[0].component.eligibility is PreflightEligibility.ERROR
    assert result.components[0].result.status is StudyStatus.ERROR
    assert result.components[0].result.evidence == (
        "DATA_ERROR",
        "CALENDAR_COVERAGE_UNKNOWN",
    )
    assert not study.calls
    assert store.lookups == []
    assert len(store.saved) == 1


def test_insufficient_warmup_uses_snapshot_bound_runner_path(tmp_path: Path) -> None:
    study = SyntheticStudy("trend.first", minimum=4)
    coordinator, store = setup(tmp_path, (study,))
    input_bars = bars("M5", count=2)
    analysis_request = request((spec("M5"),), {"M5": input_bars})
    result = coordinator.run(analysis_request)
    repeat = coordinator.run(analysis_request)
    item = result.components[0]

    assert item.component.eligibility is PreflightEligibility.INSUFFICIENT_DATA
    assert item.result.status is StudyStatus.INSUFFICIENT_DATA
    assert item.result.data_snapshot_id == compute_bar_snapshot_id(input_bars.bars)
    assert result.completeness is AnalysisCompleteness.NO_AVAILABLE_COMPONENTS
    assert result == repeat and not study.calls
    assert len(store.lookups) == 2 and len(store.saved) == 1


def test_computation_error_does_not_stop_later_components(tmp_path: Path) -> None:
    failing = SyntheticStudy("trend.first", fail=True)
    succeeding = SyntheticStudy("trend.second")
    coordinator, store = setup(tmp_path, (failing, succeeding))
    result = coordinator.run(request((spec("M5", "trend.second"), spec("M5")), {"M5": bars("M5")}))

    assert [item.result.status for item in result.components] == [
        StudyStatus.ERROR,
        StudyStatus.AVAILABLE,
    ]
    assert result.components[0].result.evidence == ("STUDY_EXECUTION_ERROR",)
    assert result.completeness is AnalysisCompleteness.PARTIAL
    assert len(failing.calls) == len(succeeding.calls) == 1
    assert len(store.saved) == 2


def test_preflight_rejects_later_invalid_component_before_any_execution(tmp_path: Path) -> None:
    study = SyntheticStudy("trend.first")
    coordinator, store = setup(tmp_path, (study,))
    invalid_failure = DataFailure(
        INSTRUMENT,
        "H1",
        CUTOFF,
        DataQualityReport(DataStatus.ERROR, CUTOFF - timedelta(seconds=1), ("BAD_DATA",)),
    )
    analysis_request = request((spec("M5"), spec("H1")), {"M5": bars("M5"), "H1": invalid_failure})

    with pytest.raises(StudyRequestError, match="precedes"):
        coordinator.run(analysis_request)
    assert not study.calls and not store.lookups and not store.saved


def test_cutoff_mismatch_and_future_bars_rejected_before_execution(tmp_path: Path) -> None:
    study = SyntheticStudy("trend.first")
    _, store = setup(tmp_path, (study,))
    valid = bars("M5")
    with pytest.raises(StudyRequestError, match="cutoff"):
        MultiTimeframeAnalysisRequest(
            INSTRUMENT,
            CUTOFF + timedelta(minutes=5),
            (spec("M5"),),
            {"M5": valid},
            FreshnessPolicy.one_timeframe_duration(),
        )
    future = (
        MarketBar(
            instrument=INSTRUMENT,
            timeframe="M5",
            timestamp_utc=CUTOFF,
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=None,
            source=SOURCE,
        ),
    )
    with pytest.raises(ValueError, match="complete after cutoff"):
        ValidatedBarSet.from_bars(future, "M5", CUTOFF + timedelta(minutes=5), cutoff_utc=CUTOFF)
    assert not study.calls and not store.lookups and not store.saved


def test_later_persistence_failure_propagates_after_first_save(tmp_path: Path) -> None:
    study = SyntheticStudy("trend.first")
    coordinator, store = setup(tmp_path, (study,), fail_save=2)
    analysis_request = request((spec("H1"), spec("M5")), {"M5": bars("M5"), "H1": bars("H1")})

    with pytest.raises(StudyPersistenceError, match="failed to persist") as caught:
        coordinator.run(analysis_request)
    assert isinstance(caught.value.__cause__, OSError)
    assert store.save_attempts == 2 and len(store.saved) == 1
    assert store.saved[0].timeframe == "M5"
    assert store.inner.find_cached_study_result(store.lookups[0]) == store.saved[0]
    assert store.inner.find_cached_study_result(store.lookups[1]) is None
