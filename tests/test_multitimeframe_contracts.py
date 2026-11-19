"""Contracts and preflight tests for independent multi-timeframe executions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
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
from kankalar_gold_ai.data.calendar import (
    CalendarClosure,
    CalendarProvenance,
    SessionCalendar,
    WeeklySession,
)
from kankalar_gold_ai.data.quality import TIMEFRAME_DURATIONS
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import SQLitePersistence
from kankalar_gold_ai.studies import (
    AnalysisCompleteness,
    CalendarAwareFreshnessPolicy,
    ComponentResultEnvelope,
    DataFailure,
    FreshnessPolicy,
    MultiTimeframeAnalysisRequest,
    MultiTimeframeAnalysisResult,
    PreflightEligibility,
    StudyConfigError,
    StudyExecutionSpec,
    StudyOutput,
    StudyRegistry,
    StudyRequestError,
    StudyRunner,
    StudyRunRequest,
    preflight_multi_timeframe_analysis,
)
from kankalar_gold_ai.studies.atr import ATRStudy
from kankalar_gold_ai.studies.base import ConfigValue

CUTOFF = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
INSTRUMENT = "TEST_GOLD"
SOURCE = "synthetic-multitimeframe"
TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1")


class ConfigurableStudy:
    version = "1.0.0"
    required_timeframes = frozenset({"M5"})

    def __init__(self, study_id: str, config_key: str = "period") -> None:
        self.study_id = study_id
        self.config_key = config_key
        self.compute_calls = 0

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        if set(raw) - {self.config_key}:
            raise StudyConfigError("unknown config")
        return {"period": raw.get(self.config_key, 2)}

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        return 2

    def compute(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, ConfigValue],
    ) -> StudyOutput:
        self.compute_calls += 1
        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            value=float(config["period"]),
            direction=Direction.NOT_APPLICABLE,
        )


class CountingStore:
    def __init__(self, path: Path) -> None:
        self.inner = SQLitePersistence(path)
        self.inner.initialize()
        self.lookups = 0
        self.saves = 0

    def save_study_result(self, result: StudyResult) -> None:
        self.saves += 1
        self.inner.save_study_result(result)

    def find_cached_study_result(self, key: StudyResultKey) -> StudyResult | None:
        self.lookups += 1
        return self.inner.find_cached_study_result(key)


def _bars(
    timeframe: str = "M5",
    *,
    count: int = 3,
    latest_close: datetime = CUTOFF,
    source: str = SOURCE,
    calendar: SessionCalendar | None = None,
) -> ValidatedBarSet:
    duration = TIMEFRAME_DURATIONS[timeframe]
    start = latest_close - duration * (count - 1) - duration
    bars = tuple(
        MarketBar(
            instrument=INSTRUMENT,
            timeframe=timeframe,
            timestamp_utc=start + duration * index,
            open=100.0 + index,
            high=102.0 + index,
            low=99.0 + index,
            close=101.0 + index,
            volume=None,
            source=source,
        )
        for index in range(count)
    )
    return ValidatedBarSet.from_bars(
        bars,
        timeframe,
        CUTOFF,
        cutoff_utc=CUTOFF,
        calendar=calendar,
    )


def _request(
    executions: tuple[StudyExecutionSpec, ...] | None = None,
    inputs: Mapping[str, ValidatedBarSet | DataFailure] | None = None,
    *,
    as_of: datetime = CUTOFF,
    policy: FreshnessPolicy | CalendarAwareFreshnessPolicy | None = None,
    calendar: SessionCalendar | None = None,
) -> MultiTimeframeAnalysisRequest:
    selected = (
        (StudyExecutionSpec("volatility.atr", "0.1.0", "M5", {"atr_period": 2}),)
        if executions is None
        else executions
    )
    supplied = {"M5": _bars()} if inputs is None else inputs
    return MultiTimeframeAnalysisRequest(
        instrument=INSTRUMENT,
        as_of_utc=as_of,
        executions=selected,
        inputs_by_timeframe=supplied,
        freshness_policy=policy or FreshnessPolicy.one_timeframe_duration(),
        session_calendar=calendar,
    )


def _runner(
    tmp_path: Path, studies: tuple[object, ...] | None = None
) -> tuple[StudyRunner, CountingStore, tuple[object, ...]]:
    implementations = studies or (ATRStudy(),)
    registry = StudyRegistry()
    for study in implementations:
        registry.register(study)  # type: ignore[arg-type]
    store = CountingStore(tmp_path / "multitimeframe.db")
    return StudyRunner(registry, store), store, implementations


@pytest.mark.parametrize(
    ("timeframes", "executions"),
    [
        (("M5",), (StudyExecutionSpec("volatility.atr", "0.1.0", "M5", {"atr_period": 2}),)),
        (
            ("M5", "H1"),
            (
                StudyExecutionSpec("volatility.atr", "0.1.0", "M5", {"atr_period": 2}),
                StudyExecutionSpec("volatility.atr", "0.1.0", "H1", {"atr_period": 2}),
            ),
        ),
    ],
)
def test_explicit_nonempty_subsets_preflight(
    tmp_path: Path,
    timeframes: tuple[str, ...],
    executions: tuple[StudyExecutionSpec, ...],
) -> None:
    runner, store, _studies = _runner(tmp_path)
    request = _request(
        executions,
        {timeframe: _bars(timeframe) for timeframe in timeframes},
    )

    prepared = preflight_multi_timeframe_analysis(request, runner)

    assert tuple(component.timeframe for component in prepared.components) == timeframes
    assert {component.eligibility for component in prepared.components} == {
        PreflightEligibility.ELIGIBLE
    }
    assert prepared.input_snapshot_id.startswith("analysis-input-v1:sha256:")
    assert store.lookups == store.saves == 0


def test_empty_execution_subset_is_rejected() -> None:
    with pytest.raises(StudyRequestError, match="non-empty"):
        _request((), {})


def test_duplicate_normalized_execution_identity_is_rejected(tmp_path: Path) -> None:
    runner, store, study_set = _runner(tmp_path, (ConfigurableStudy("trend.first"),))
    spec = StudyExecutionSpec("trend.first", "1.0.0", "M5", {"period": 2})
    request = _request((spec, spec))

    with pytest.raises(StudyRequestError, match="duplicate normalized"):
        preflight_multi_timeframe_analysis(request, runner)

    assert study_set[0].compute_calls == 0  # type: ignore[attr-defined]
    assert store.lookups == store.saves == 0


def test_same_timeframe_supports_distinct_studies_and_normalized_configs(
    tmp_path: Path,
) -> None:
    runner, store, _studies = _runner(
        tmp_path,
        (
            ConfigurableStudy("trend.first"),
            ConfigurableStudy("structure.second"),
        ),
    )
    request = _request(
        (
            StudyExecutionSpec("trend.first", "1.0.0", "M5", {"period": 2}),
            StudyExecutionSpec("trend.first", "1.0.0", "M5", {"period": 3}),
            StudyExecutionSpec("structure.second", "1.0.0", "M5", {"period": 2}),
        )
    )

    result = preflight_multi_timeframe_analysis(request, runner)

    assert len(result.components) == 3
    assert {component.timeframe for component in result.components} == {"M5"}
    assert store.lookups == store.saves == 0


def test_duplicate_identity_uses_normalized_not_raw_configuration(tmp_path: Path) -> None:
    runner, _store, _studies = _runner(
        tmp_path, (ConfigurableStudy("trend.first", config_key="raw"),)
    )
    # The study normalizes both omitted and explicit raw=2 to the same period=2.
    request = _request(
        (
            StudyExecutionSpec("trend.first", "1.0.0", "M5", {}),
            StudyExecutionSpec("trend.first", "1.0.0", "M5", {"raw": 2}),
        )
    )

    with pytest.raises(StudyRequestError, match="duplicate normalized"):
        preflight_multi_timeframe_analysis(request, runner)


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({}, "exactly match"),
        ({"M5": _bars(), "H1": _bars("H1")}, "exactly match"),
        ({"NOPE": _bars()}, "unsupported timeframe key"),
    ],
)
def test_input_mapping_must_match_requested_timeframes(
    inputs: Mapping[str, ValidatedBarSet | DataFailure], message: str
) -> None:
    with pytest.raises(StudyRequestError, match=message):
        _request(inputs=inputs)


def test_timeframe_input_is_a_single_tagged_value() -> None:
    with pytest.raises(StudyRequestError, match="exactly one"):
        _request(inputs={"M5": object()})  # type: ignore[dict-item]

    failure = DataFailure(
        instrument=INSTRUMENT,
        timeframe="H1",
        as_of_utc=CUTOFF,
        report=DataQualityReport(DataStatus.UNAVAILABLE, CUTOFF, ("NO_SOURCE",)),
    )
    with pytest.raises(StudyRequestError, match="does not match"):
        _request(inputs={"M5": failure})


def test_data_failure_cutoff_is_compared_as_an_utc_instant(tmp_path: Path) -> None:
    runner, store, _studies = _runner(tmp_path, (ConfigurableStudy("trend.first"),))
    failure = DataFailure(
        instrument=INSTRUMENT,
        timeframe="M5",
        as_of_utc=CUTOFF.astimezone(timezone(timedelta(hours=3))),
        report=DataQualityReport(
            DataStatus.UNAVAILABLE,
            CUTOFF,
            ("NO_SOURCE",),
        ),
    )
    request = _request(
        (StudyExecutionSpec("trend.first", "1.0.0", "M5"),),
        {"M5": failure},
    )

    prepared = preflight_multi_timeframe_analysis(request, runner)

    assert prepared.components[0].eligibility is PreflightEligibility.UNAVAILABLE
    assert prepared.components[0].prepared.data_snapshot_id is None
    assert store.lookups == store.saves == 0


def test_unsupported_execution_timeframe_is_rejected() -> None:
    with pytest.raises(StudyRequestError, match="unsupported"):
        StudyExecutionSpec("volatility.atr", "0.1.0", "W1")


def test_validated_inputs_must_share_one_source(tmp_path: Path) -> None:
    runner, store, _studies = _runner(tmp_path)
    request = _request(
        (
            StudyExecutionSpec("volatility.atr", "0.1.0", "M5", {"atr_period": 2}),
            StudyExecutionSpec("volatility.atr", "0.1.0", "H1", {"atr_period": 2}),
        ),
        {"M5": _bars(), "H1": _bars("H1", source="other-source")},
    )

    with pytest.raises(StudyRequestError, match="one source"):
        preflight_multi_timeframe_analysis(request, runner)
    assert store.lookups == store.saves == 0


def test_common_cutoff_is_normalized_to_utc_and_enforced() -> None:
    offset_cutoff = CUTOFF.astimezone(timezone(timedelta(hours=3)))
    request = _request(as_of=offset_cutoff)
    assert request.as_of_utc == CUTOFF

    with pytest.raises(StudyRequestError, match="cutoff"):
        _request(as_of=CUTOFF + timedelta(seconds=1))


def test_freshness_policy_is_fixed_immutable_and_explicit() -> None:
    policy = FreshnessPolicy.one_timeframe_duration()
    assert policy.max_age_by_timeframe["H4"] == timedelta(hours=4)
    with pytest.raises(TypeError):
        policy.max_age_by_timeframe["H4"] = timedelta(hours=3)  # type: ignore[index]
    with pytest.raises(StudyRequestError, match="identity"):
        FreshnessPolicy("other-policy", "1.0.0", policy.max_age_by_timeframe)
    wrong_ages = dict(policy.max_age_by_timeframe)
    wrong_ages["M5"] = timedelta(minutes=10)
    with pytest.raises(StudyRequestError, match="one timeframe duration"):
        FreshnessPolicy(policy.policy_id, policy.version, wrong_ages)
    wrong_timeframes = dict(policy.max_age_by_timeframe)
    wrong_timeframes.pop("D1")
    with pytest.raises(StudyRequestError, match="exactly the six"):
        FreshnessPolicy(policy.policy_id, policy.version, wrong_timeframes)


def test_freshness_age_boundary_is_inclusive_and_stale_has_no_snapshot(
    tmp_path: Path,
) -> None:
    runner, _store, _studies = _runner(tmp_path)
    duration = TIMEFRAME_DURATIONS["M5"]
    fresh_bars = _bars(latest_close=CUTOFF - duration)
    fresh_request = _request(inputs={"M5": fresh_bars})
    fresh = preflight_multi_timeframe_analysis(fresh_request, runner)
    assert fresh.components[0].bar_age == duration
    assert fresh.components[0].eligibility is PreflightEligibility.ELIGIBLE

    stale_bars = _bars(latest_close=CUTOFF - duration - timedelta(microseconds=1))
    stale_request = _request(inputs={"M5": stale_bars})
    stale = preflight_multi_timeframe_analysis(stale_request, runner)
    assert stale.components[0].eligibility is PreflightEligibility.STALE
    assert stale.components[0].prepared.data_snapshot_id is not None


def _always_open_calendar(
    *,
    start: datetime = CUTOFF - timedelta(days=1),
    end: datetime = CUTOFF + timedelta(days=1),
    closure: tuple[datetime, datetime] | None = None,
    version: str = "calendar-v1",
    timezone_name: str = "UTC",
    provider_id: str = SOURCE,
    instrument_id: str = INSTRUMENT,
) -> SessionCalendar:
    closures = (
        ()
        if closure is None
        else (CalendarClosure(closure[0], closure[1], "synthetic closed interval"),)
    )
    return SessionCalendar(
        calendar_id="synthetic.instrument",
        version=version,
        provider_id=provider_id,
        instrument_id=instrument_id,
        provenance=CalendarProvenance(
            source="synthetic-test-fixture",
            reference="tests/test_multitimeframe_contracts.py",
        ),
        timezone_name=timezone_name,
        effective_start_utc=start,
        effective_end_utc=end,
        coverage_start_utc=start,
        coverage_end_utc=end,
        weekly_sessions=tuple(WeeklySession(day, time(0), time(23, 59)) for day in range(7)),
        exceptional_closures=closures,
    )


def test_calendar_freshness_uses_independent_wall_and_open_market_ages(
    tmp_path: Path,
) -> None:
    closure = (CUTOFF - timedelta(hours=1), CUTOFF)
    calendar = _always_open_calendar(closure=closure)
    bar_set = _bars(
        latest_close=CUTOFF - timedelta(hours=2),
        calendar=calendar,
    )
    policy = CalendarAwareFreshnessPolicy(
        max_wall_clock_age=timedelta(hours=3),
        max_open_market_age=timedelta(minutes=90),
    )
    runner, _store, _studies = _runner(tmp_path)

    preflight = preflight_multi_timeframe_analysis(
        _request(
            inputs={"M5": bar_set},
            policy=policy,
            calendar=calendar,
        ),
        runner,
    )

    component = preflight.components[0]
    assert component.bar_age == timedelta(hours=2)
    assert component.open_market_age == timedelta(hours=1)
    assert component.eligibility is PreflightEligibility.ELIGIBLE
    assert calendar.canonical_identity()["id"] in preflight.input_manifest_json
    assert "configuration_sha256" in preflight.input_manifest_json
    assert '"timezone":"UTC"' in preflight.input_manifest_json
    assert '"provenance_source":"synthetic-test-fixture"' in preflight.input_manifest_json
    assert "max_open_market_age_microseconds" in preflight.input_manifest_json


def test_calendar_freshness_keeps_hard_wall_clock_limit_and_unknown_fails_closed(
    tmp_path: Path,
) -> None:
    runner, _store, _studies = _runner(tmp_path)
    calendar = _always_open_calendar(closure=(CUTOFF - timedelta(hours=1), CUTOFF))
    hard_wall_policy = CalendarAwareFreshnessPolicy(
        max_wall_clock_age=timedelta(minutes=90),
        max_open_market_age=timedelta(hours=3),
    )
    hard_wall = preflight_multi_timeframe_analysis(
        _request(
            inputs={"M5": _bars(latest_close=CUTOFF - timedelta(hours=2), calendar=calendar)},
            policy=hard_wall_policy,
            calendar=calendar,
        ),
        runner,
    )
    assert hard_wall.components[0].eligibility is PreflightEligibility.STALE
    assert hard_wall.components[0].bar_age == timedelta(hours=2)

    limited_calendar = _always_open_calendar(
        end=CUTOFF - timedelta(hours=1),
        version="limited-v1",
    )
    unknown_bars = _bars(
        latest_close=CUTOFF - timedelta(hours=2),
        calendar=limited_calendar,
    )
    unknown = preflight_multi_timeframe_analysis(
        _request(
            inputs={"M5": unknown_bars},
            policy=CalendarAwareFreshnessPolicy(
                max_wall_clock_age=timedelta(hours=3),
                max_open_market_age=timedelta(hours=3),
            ),
            calendar=limited_calendar,
        ),
        runner,
    )
    assert unknown.components[0].eligibility is PreflightEligibility.ERROR
    assert unknown.components[0].freshness_reasons == ("CALENDAR_COVERAGE_UNKNOWN",)


def test_calendar_aware_request_rejects_mismatched_calendar_configuration() -> None:
    calendar = _always_open_calendar()
    different = replace(
        calendar,
        weekly_sessions=(WeeklySession(0, time(9), time(17)),),
    )
    bar_set = _bars(calendar=calendar)
    policy = CalendarAwareFreshnessPolicy(
        max_wall_clock_age=timedelta(hours=3),
        max_open_market_age=timedelta(hours=3),
    )

    with pytest.raises(StudyRequestError, match="exact requested calendar identity"):
        _request(inputs={"M5": bar_set}, policy=policy, calendar=different)


def test_calendar_aware_request_rejects_timezone_mismatch() -> None:
    utc_calendar = _always_open_calendar()
    london_calendar = _always_open_calendar(timezone_name="Europe/London")
    bar_set = _bars(calendar=utc_calendar)
    policy = CalendarAwareFreshnessPolicy(
        max_wall_clock_age=timedelta(hours=3),
        max_open_market_age=timedelta(hours=3),
    )

    with pytest.raises(StudyRequestError, match="exact requested calendar identity"):
        _request(inputs={"M5": bar_set}, policy=policy, calendar=london_calendar)


@pytest.mark.parametrize(
    ("provider_id", "instrument_id"),
    [
        ("synthetic-other-provider", INSTRUMENT),
        (SOURCE, "SYNTHETIC_OTHER_INSTRUMENT"),
    ],
)
def test_validated_bars_reject_calendar_scope_mismatch(
    provider_id: str,
    instrument_id: str,
) -> None:
    calendar = _always_open_calendar(provider_id=provider_id, instrument_id=instrument_id)

    with pytest.raises(ValueError, match="calendar scope mismatch"):
        _bars(calendar=calendar)


def test_legacy_freshness_records_calendar_validated_input_identity(
    tmp_path: Path,
) -> None:
    runner, _store, _studies = _runner(tmp_path)
    calendar = _always_open_calendar()
    alternate_calendar = replace(calendar, version="calendar-v2")
    policy = FreshnessPolicy.one_timeframe_duration()

    first = preflight_multi_timeframe_analysis(
        _request(inputs={"M5": _bars(calendar=calendar)}, policy=policy),
        runner,
    )
    second = preflight_multi_timeframe_analysis(
        _request(inputs={"M5": _bars(calendar=alternate_calendar)}, policy=policy),
        runner,
    )

    manifest = json.loads(first.input_manifest_json)
    assert manifest["session_calendar"] == calendar.canonical_identity()
    assert first.input_snapshot_id != second.input_snapshot_id
    assert manifest["freshness_policy"]["id"] == "one-timeframe-duration"
    assert manifest["freshness_policy"]["version"] == "1.0.0"


def test_legacy_freshness_rejects_mixed_calendar_identities() -> None:
    first_calendar = _always_open_calendar()
    second_calendar = replace(first_calendar, version="calendar-v2")
    executions = (
        StudyExecutionSpec("volatility.atr", "0.1.0", "M5", {"atr_period": 2}),
        StudyExecutionSpec("volatility.atr", "0.1.0", "H1", {"atr_period": 2}),
    )

    with pytest.raises(StudyRequestError, match="one consistent calendar identity"):
        _request(
            executions,
            {
                "M5": _bars("M5", calendar=first_calendar),
                "H1": _bars("H1", calendar=second_calendar),
            },
            policy=FreshnessPolicy.one_timeframe_duration(),
        )


def test_legacy_freshness_rejects_mixed_calendar_and_unscoped_inputs() -> None:
    calendar = _always_open_calendar()
    executions = (
        StudyExecutionSpec("volatility.atr", "0.1.0", "M5", {"atr_period": 2}),
        StudyExecutionSpec("volatility.atr", "0.1.0", "H1", {"atr_period": 2}),
    )

    with pytest.raises(StudyRequestError, match="cannot be mixed"):
        _request(
            executions,
            {
                "M5": _bars("M5", calendar=calendar),
                "H1": _bars("H1"),
            },
            policy=FreshnessPolicy.one_timeframe_duration(),
        )


def test_preflight_validates_every_execution_before_any_child_execution_or_persistence(
    tmp_path: Path,
) -> None:
    study = ConfigurableStudy("trend.first")
    runner, store, _studies = _runner(tmp_path, (study,))
    request = _request(
        (
            StudyExecutionSpec("trend.first", "1.0.0", "M5", {"period": 2}),
            StudyExecutionSpec("trend.missing", "1.0.0", "M5", {}),
        )
    )

    with pytest.raises(LookupError):
        preflight_multi_timeframe_analysis(request, runner)

    assert study.compute_calls == 0
    assert store.lookups == store.saves == 0


def test_runner_prepare_is_non_persisting_and_normalizes_the_run(tmp_path: Path) -> None:
    runner, store, _studies = _runner(tmp_path, (ConfigurableStudy("trend.first"),))
    bars = _bars()
    request = StudyRunRequest("trend.first", "1.0.0", INSTRUMENT, "M5", CUTOFF, {"period": 4})
    prepared = runner.prepare(request, bars)

    assert prepared.normalized_config == {"period": 4}
    assert prepared.min_bars == 2
    assert prepared.data_snapshot_id is not None
    assert prepared.input_window_end_utc == CUTOFF
    assert prepared.latest_completed_at_utc == CUTOFF
    assert store.lookups == store.saves == 0

    result = runner.run(request, bars)
    assert result.status is StudyStatus.AVAILABLE
    assert store.saves == 1


def test_runner_uses_calendar_local_d1_completion_across_25_hour_day(
    tmp_path: Path,
) -> None:
    calendar = _always_open_calendar(
        timezone_name="Europe/London",
        start=datetime(2026, 10, 23, 23, tzinfo=timezone.utc),
        end=datetime(2026, 10, 28, tzinfo=timezone.utc),
    )
    previous_open = datetime(2026, 10, 23, 23, tzinfo=timezone.utc)
    daily_open = datetime(2026, 10, 24, 23, tzinfo=timezone.utc)
    completion = datetime(2026, 10, 26, 0, tzinfo=timezone.utc)
    bars = ValidatedBarSet.from_bars(
        (
            MarketBar(INSTRUMENT, "D1", previous_open, 100, 102, 99, 101, None, SOURCE),
            MarketBar(INSTRUMENT, "D1", daily_open, 101, 103, 100, 102, None, SOURCE),
        ),
        "D1",
        completion,
        cutoff_utc=completion,
        calendar=calendar,
    )
    runner, _store, _studies = _runner(tmp_path)
    request = StudyRunRequest(
        "volatility.atr",
        "0.1.0",
        INSTRUMENT,
        "D1",
        completion,
        {"atr_period": 2},
    )

    prepared = runner.prepare(request, bars)

    assert prepared.latest_completed_at_utc == completion
    assert prepared.input_window_end_utc == completion


def test_input_manifest_is_order_independent_and_has_no_final_result_status(
    tmp_path: Path,
) -> None:
    runner, _store, _studies = _runner(tmp_path)
    specs = (
        StudyExecutionSpec("volatility.atr", "0.1.0", "M5", {"atr_period": 2}),
        StudyExecutionSpec("volatility.atr", "0.1.0", "H1", {"atr_period": 2}),
    )
    inputs = {"M5": _bars(), "H1": _bars("H1")}
    first = preflight_multi_timeframe_analysis(_request(specs, inputs), runner)
    second = preflight_multi_timeframe_analysis(_request(tuple(reversed(specs)), inputs), runner)

    assert first.input_manifest_json == second.input_manifest_json
    assert first.input_snapshot_id == second.input_snapshot_id
    assert '"status"' not in first.input_manifest_json


def test_analysis_result_identity_is_separate_and_includes_final_status(
    tmp_path: Path,
) -> None:
    runner, _store, _studies = _runner(tmp_path)
    request = _request(
        (
            StudyExecutionSpec("volatility.atr", "0.1.0", "M5", {"atr_period": 2}),
            StudyExecutionSpec("volatility.atr", "0.1.0", "M5", {"atr_period": 3}),
        ),
        {"M5": _bars(count=5)},
    )
    preflight = preflight_multi_timeframe_analysis(request, runner)
    bar_set = request.inputs_by_timeframe["M5"]
    assert isinstance(bar_set, ValidatedBarSet)
    results = tuple(
        ComponentResultEnvelope(
            component,
            runner.run(component.prepared.request, bar_set),
        )
        for component in preflight.components
    )
    available = MultiTimeframeAnalysisResult(preflight, results)
    unavailable_result = replace(
        results[1].result,
        status=StudyStatus.UNAVAILABLE,
        state=None,
        value=None,
        normalized_value=None,
        direction=Direction.NOT_APPLICABLE,
        uncertainty=None,
    )
    partial = MultiTimeframeAnalysisResult(
        preflight,
        (
            results[0],
            ComponentResultEnvelope(preflight.components[1], unavailable_result),
        ),
    )

    assert available.completeness is AnalysisCompleteness.COMPLETE
    assert partial.completeness is AnalysisCompleteness.PARTIAL
    assert available.result_identity.startswith("analysis-result-v1:sha256:")
    assert available.preflight.input_snapshot_id != available.result_identity
    assert '"AVAILABLE"' in available.result_manifest_json
    assert '"UNAVAILABLE"' in partial.result_manifest_json
