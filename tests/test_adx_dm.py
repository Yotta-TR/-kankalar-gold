"""Synthetic unit and integration tests for Wilder DMI/ADX."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

from kankalar_gold_ai.core.contracts import (
    DataQualityReport,
    DataStatus,
    Direction,
    MarketBar,
    StudyResult,
    StudyStatus,
)
from kankalar_gold_ai.core.study_results import StudyResultKey, canonical_inputs_json
from kankalar_gold_ai.data.errors import HistoricalDataQualityError
from kankalar_gold_ai.data.quality import TIMEFRAME_DURATIONS
from kankalar_gold_ai.data.snapshot import compute_bar_snapshot_id
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import SQLitePersistence
from kankalar_gold_ai.studies import (
    DataFailure,
    FreshnessPolicy,
    MultiTimeframeAnalysisCoordinator,
    MultiTimeframeAnalysisRequest,
    StudyConfigError,
    StudyExecutionSpec,
    StudyOutput,
    StudyRegistry,
    StudyRunner,
    StudyRunRequest,
)
from kankalar_gold_ai.studies.adx_dm import ADXDMIStudy, _metrics
from kankalar_gold_ai.studies.base import ConfigValue
from tests.fixtures.point_in_time import (
    assert_no_lookahead,
    blind,
    build_universe,
    end_of,
)

CUTOFF = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1")
CONFIG: dict[str, ConfigValue] = {"adx_period": 2}
PLUS_UP = ((10.0, 8.0, 9.0), (11.0, 9.0, 10.0), (12.0, 10.0, 11.0), (13.0, 11.0, 12.0))
MINUS_DOWN = (
    (13.0, 11.0, 12.0),
    (12.0, 10.0, 11.0),
    (11.0, 9.0, 10.0),
    (10.0, 8.0, 9.0),
)


def bars(
    rows: tuple[tuple[float, float, float], ...],
    timeframe: str = "M5",
    *,
    cutoff: datetime = CUTOFF,
) -> tuple[MarketBar, ...]:
    duration = TIMEFRAME_DURATIONS[timeframe]
    return tuple(
        MarketBar(
            instrument="SYNTHETIC_GOLD",
            timeframe=timeframe,
            timestamp_utc=cutoff - duration * (len(rows) - index),
            open=close,
            high=high,
            low=low,
            close=close,
            volume=None,
            source="synthetic-adx",
        )
        for index, (high, low, close) in enumerate(rows)
    )


def validated(
    items: tuple[MarketBar, ...],
    *,
    cutoff: datetime = CUTOFF,
    expected_open: Callable[[datetime], bool] | None = None,
) -> ValidatedBarSet:
    return ValidatedBarSet.from_bars(
        items,
        items[0].timeframe,
        cutoff,
        cutoff_utc=cutoff,
        expected_open=expected_open,
    )


def request(
    timeframe: str = "M5",
    *,
    config: Mapping[str, ConfigValue] = CONFIG,
    cutoff: datetime = CUTOFF,
) -> StudyRunRequest:
    return StudyRunRequest("trend.adx_dm", "0.1.0", "SYNTHETIC_GOLD", timeframe, cutoff, config)


class CountingADX(ADXDMIStudy):
    def __init__(self) -> None:
        self.calls: list[tuple[MarketBar, ...]] = []

    def compute(
        self, items: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        self.calls.append(items)
        return super().compute(items, config)


def setup(
    tmp_path: Path, study: ADXDMIStudy | None = None, *, database: str = "adx.db"
) -> tuple[StudyRunner, SQLitePersistence]:
    registry = StudyRegistry()
    registry.register(study or ADXDMIStudy())
    store = SQLitePersistence(tmp_path / database)
    store.initialize()
    return StudyRunner(registry, store), store


def cache_key(result: StudyResult) -> StudyResultKey:
    assert result.data_snapshot_id is not None
    return StudyResultKey(
        result.study_id,
        result.version,
        result.instrument,
        result.timeframe,
        result.as_of_utc,
        canonical_inputs_json(result.inputs),
        result.data_snapshot_id,
        calendar_context_id=dict(result.provenance)["calendar_context_id"],
    )


def assert_no_values(result: StudyResult) -> None:
    assert result.state is None
    assert result.value is None
    assert result.normalized_value is None
    assert result.uncertainty is None
    assert result.direction is Direction.NOT_APPLICABLE


def evidence_values(output: StudyOutput) -> dict[str, str]:
    return dict(item.split("=", 1) for item in output.evidence if "=" in item)


@pytest.mark.parametrize(
    ("rows", "state", "direction", "plus_di", "minus_di", "dominance"),
    [
        (PLUS_UP, "PLUS_DI_DOMINANT", Direction.BULLISH, 50.0, 0.0, 1.0),
        (MINUS_DOWN, "MINUS_DI_DOMINANT", Direction.BEARISH, 0.0, 50.0, -1.0),
    ],
)
def test_hand_calculated_directional_examples(
    rows: tuple[tuple[float, float, float], ...],
    state: str,
    direction: Direction,
    plus_di: float,
    minus_di: float,
    dominance: float,
) -> None:
    output = ADXDMIStudy().compute(bars(rows), CONFIG)
    measured = evidence_values(output)

    assert output.status is StudyStatus.AVAILABLE
    assert output.state == state
    assert output.direction is direction
    assert output.value == 100.0
    assert output.normalized_value == dominance
    assert float(measured["latest_plus_di"]) == plus_di
    assert float(measured["latest_minus_di"]) == minus_di
    assert float(measured["latest_dx"]) == 100.0
    assert output.uncertainty is None


def test_wilder_seed_recurrence_and_adx_initialization_indices() -> None:
    rows = (
        (10.0, 8.0, 9.0),
        (11.0, 9.0, 10.0),
        (12.0, 10.0, 11.0),
        (11.0, 9.0, 10.0),
        (10.0, 8.0, 9.0),
        (11.0, 9.0, 10.0),
    )
    study = ADXDMIStudy()

    first_adx = study.compute(bars(rows[:4]), CONFIG)
    after_one_more = study.compute(bars(rows[:5]), CONFIG)
    after_two_more = study.compute(bars(rows), CONFIG)

    # Seed at bar 2: +DM=2, -DM=0, TR=4; DX values at bars 2 and 3 are 100 and 0.
    assert first_adx.value == 50.0
    assert first_adx.normalized_value == 0.0
    assert first_adx.state == "DI_BALANCED"
    # Bar 4 recurrence: +DM=0.5, -DM=1.5, TR=4; DX=50 and ADX=50.
    assert float(evidence_values(after_one_more)["latest_plus_di"]) == 12.5
    assert float(evidence_values(after_one_more)["latest_minus_di"]) == 37.5
    assert float(evidence_values(after_one_more)["latest_dx"]) == pytest.approx(50.0)
    assert after_one_more.value == pytest.approx(50.0)
    # Bar 5 recurrence: +DM=1.25, -DM=0.75, TR=4; DX=25 and ADX=37.5.
    assert float(evidence_values(after_two_more)["latest_plus_di"]) == 31.25
    assert float(evidence_values(after_two_more)["latest_minus_di"]) == 18.75
    assert float(evidence_values(after_two_more)["latest_dx"]) == pytest.approx(25.0)
    assert after_two_more.value == pytest.approx(37.5)


@pytest.mark.parametrize(
    ("rows", "expected_state", "expected_normalized", "expected_direction", "zero_case"),
    [
        (
            ((10.0, 8.0, 9.0), (11.0, 7.0, 9.0), (12.0, 6.0, 9.0), (13.0, 5.0, 9.0)),
            "DI_BALANCED",
            None,
            Direction.NEUTRAL,
            "ZERO_DM",
        ),
        (
            ((10.0, 8.0, 8.0), (9.0, 8.0, 9.0), (9.0, 8.0, 9.0), (9.0, 8.0, 9.0)),
            "DI_BALANCED",
            None,
            Direction.NEUTRAL,
            "ZERO_DM",
        ),
        (
            ((10.0, 10.0, 10.0), (10.0, 10.0, 10.0), (10.0, 10.0, 10.0), (10.0, 10.0, 10.0)),
            "DI_BALANCED",
            None,
            Direction.NEUTRAL,
            "ZERO_TR",
        ),
    ],
)
def test_zero_directional_movement_and_zero_true_range_policy(
    rows: tuple[tuple[float, float, float], ...],
    expected_state: str,
    expected_normalized: float | None,
    expected_direction: Direction,
    zero_case: str,
) -> None:
    output = ADXDMIStudy().compute(bars(rows), CONFIG)
    assert output.state == expected_state
    assert output.normalized_value is expected_normalized
    assert output.direction is expected_direction
    assert output.value == 0.0
    assert evidence_values(output)["latest_zero_denominator_case"] == zero_case


def test_equal_positive_movement_produces_zero_dm_and_balanced_output() -> None:
    rows = ((10.0, 8.0, 9.0), (11.0, 7.0, 9.0), (12.0, 6.0, 9.0), (13.0, 5.0, 9.0))
    output = ADXDMIStudy().compute(bars(rows), CONFIG)

    assert evidence_values(output)["latest_zero_denominator_case"] == "ZERO_DM"
    assert output.normalized_value is None
    assert output.value == 0.0
    assert output.state == "DI_BALANCED"


def test_equal_positive_di_values_are_balanced_not_zero_dm() -> None:
    rows = (
        (10.0, 8.0, 9.0),
        (11.0, 9.0, 10.0),
        (10.0, 8.0, 9.0),
        (10.0, 8.0, 9.0),
    )
    output = ADXDMIStudy().compute(bars(rows), CONFIG)
    measured = evidence_values(output)

    assert measured["latest_zero_denominator_case"] == "NONE"
    assert float(measured["latest_plus_di"]) == float(measured["latest_minus_di"]) > 0
    assert output.normalized_value == 0.0
    assert output.state == "DI_BALANCED"
    assert output.direction is Direction.NEUTRAL


def test_zero_raw_dm_does_not_force_dx_or_adx_to_decay() -> None:
    continued_flat = PLUS_UP + ((13.0, 11.0, 12.0), (13.0, 11.0, 12.0))
    output = ADXDMIStudy().compute(bars(continued_flat), CONFIG)
    measured = evidence_values(output)

    assert float(measured["latest_dx"]) == 100.0
    assert output.value == 100.0
    assert output.state == "PLUS_DI_DOMINANT"


def test_zero_dx_is_included_in_adx_recurrence() -> None:
    rows = PLUS_UP + (
        (12.0, 10.0, 11.0),
        (12.0, 10.0, 11.0),
    )
    output = ADXDMIStudy().compute(bars(rows), CONFIG)

    assert float(evidence_values(output)["latest_dx"]) == 0.0
    assert output.value == 25.0
    assert output.normalized_value == 0.0
    assert output.state == "DI_BALANCED"


def test_zero_tr_with_nonzero_dm_is_an_arithmetic_invariant_failure() -> None:
    with pytest.raises(ArithmeticError, match="zero smoothed TR"):
        _metrics(1.0, 0.0, 0.0)


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"adx_period": 2}, {"adx_period": 2}),
        ({"adx_period": 1_000_000}, {"adx_period": 1_000_000}),
    ],
)
def test_configuration_normalization_has_no_period_cap(
    config: dict[str, ConfigValue], expected: dict[str, ConfigValue]
) -> None:
    study = ADXDMIStudy()
    assert study.validate_config(config) == expected
    assert study.min_bars(config) == 2 * config["adx_period"]


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"adx_period": 2, "other": 1},
        {"adx_period": True},
        {"adx_period": 2.0},
        {"adx_period": "2"},
        {"adx_period": 0},
        {"adx_period": -1},
        {"adx_period": 1},
        {"adx_period": None},
    ],
)
def test_invalid_configuration_is_rejected(config: dict[str, ConfigValue]) -> None:
    with pytest.raises(StudyConfigError):
        ADXDMIStudy().validate_config(config)


def test_supported_timeframes_and_snapshot_bound_warmup(tmp_path: Path) -> None:
    study = CountingADX()
    runner, _store = setup(tmp_path, study)
    assert study.required_timeframes == frozenset(TIMEFRAMES)

    short = validated(bars(PLUS_UP[:3]))
    insufficient = runner.run(request(), short)
    assert insufficient.status is StudyStatus.INSUFFICIENT_DATA
    assert insufficient.data_snapshot_id == compute_bar_snapshot_id(short.bars)
    assert_no_values(insufficient)
    assert study.calls == []

    exact = validated(bars(PLUS_UP))
    available = runner.run(request(), exact)
    assert available.status is StudyStatus.AVAILABLE
    assert available.timeframe == "M5"
    assert study.calls == [exact.bars]


def test_subnormal_tr_and_dm_remain_computable() -> None:
    tiny = math.nextafter(0.0, 1.0)
    rows = (
        (4 * tiny, tiny, 2 * tiny),
        (5 * tiny, 2 * tiny, 3 * tiny),
        (6 * tiny, 3 * tiny, 4 * tiny),
        (7 * tiny, 4 * tiny, 5 * tiny),
    )
    output = ADXDMIStudy().compute(bars(rows), CONFIG)
    assert output.status is StudyStatus.AVAILABLE
    assert output.normalized_value == 1.0
    assert 0.0 <= output.value <= 100.0


def test_dominance_survives_di_underflow_against_large_true_range() -> None:
    tiny = math.nextafter(0.0, 1.0)
    rows = (
        (1e307, 2 * tiny, 2 * tiny),
        (1e307, tiny, tiny),
        (1e307, tiny, tiny),
        (1e307, tiny, tiny),
    )
    output = ADXDMIStudy().compute(bars(rows), CONFIG)
    measured = evidence_values(output)
    evidence_keys = (
        "latest_smoothed_plus_dm",
        "latest_smoothed_minus_dm",
        "latest_smoothed_tr",
    )
    assert all(key in measured for key in evidence_keys)
    smoothed_plus_dm, smoothed_minus_dm, smoothed_tr = (
        float(measured[key]) for key in evidence_keys
    )
    assert all(math.isfinite(value) for value in (smoothed_plus_dm, smoothed_minus_dm, smoothed_tr))

    assert output.normalized_value == -1.0
    assert float(measured["latest_minus_di"]) == 0.0
    assert smoothed_plus_dm == 0.0
    assert smoothed_minus_dm > 0.0
    assert smoothed_tr > 1e307
    assert output.state == "MINUS_DI_DOMINANT"
    assert output.direction is Direction.BEARISH


def test_large_finite_prices_preserve_output_bounds() -> None:
    scale = 1e150
    step = 1e140
    rows = tuple(
        (scale + index * step, scale - 2 * step + index * step, scale - step + index * step)
        for index in range(4)
    )
    output = ADXDMIStudy().compute(bars(rows), CONFIG)
    assert output.value is not None and 0.0 <= output.value <= 100.0
    assert output.normalized_value is not None
    assert -1.0 <= output.normalized_value <= 1.0


def test_seed_overflow_becomes_snapshot_bound_runner_error(tmp_path: Path) -> None:
    maximum = sys.float_info.max
    rows = tuple((maximum, 1.0, 1.0) for _ in range(4))
    input_bars = validated(bars(rows))
    runner, _store = setup(tmp_path)

    result = runner.run(request(), input_bars)

    assert result.status is StudyStatus.ERROR
    assert result.data_snapshot_id == compute_bar_snapshot_id(input_bars.bars)
    assert result.evidence == ("STUDY_EXECUTION_ERROR",)
    assert_no_values(result)


def test_non_finite_and_invalid_ohlc_are_rejected_by_validated_input() -> None:
    good = bars(PLUS_UP)
    invalid = (
        MarketBar(
            instrument="SYNTHETIC_GOLD",
            timeframe="M5",
            timestamp_utc=good[0].timestamp_utc,
            open=10.0,
            high=float("nan"),
            low=8.0,
            close=9.0,
            volume=None,
            source="synthetic-adx",
        ),
        *good[1:],
    )
    with pytest.raises(HistoricalDataQualityError):
        validated(invalid)
    with pytest.raises(ValueError):
        MarketBar(
            instrument="SYNTHETIC_GOLD",
            timeframe="M5",
            timestamp_utc=good[3].timestamp_utc,
            open=12.0,
            high=11.0,
            low=10.0,
            close=12.0,
            volume=None,
            source="synthetic-adx",
        )


def test_unknown_gaps_fail_and_accepted_closures_use_adjacent_observations() -> None:
    continuous = bars(PLUS_UP + ((14.0, 12.0, 13.0),))
    observed = (continuous[0], continuous[2], continuous[3], continuous[4])

    with pytest.raises(HistoricalDataQualityError):
        validated(observed)

    accepted = validated(observed, expected_open=lambda _timestamp: False)
    output = ADXDMIStudy().compute(accepted.bars, CONFIG)
    contiguous_observed = tuple(
        MarketBar(
            instrument=item.instrument,
            timeframe=item.timeframe,
            timestamp_utc=continuous[0].timestamp_utc + TIMEFRAME_DURATIONS["M5"] * index,
            open=item.open,
            high=item.high,
            low=item.low,
            close=item.close,
            volume=None,
            source=item.source,
        )
        for index, item in enumerate(observed)
    )
    assert len(accepted.bars) == 4
    assert output == ADXDMIStudy().compute(contiguous_observed, CONFIG)


@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_each_timeframe_runs_independently_with_own_cache_identity(
    tmp_path: Path, timeframe: str
) -> None:
    study = CountingADX()
    runner, store = setup(tmp_path, study)
    input_bars = validated(bars(PLUS_UP, timeframe))

    first = runner.run(request(timeframe), input_bars)
    repeated = runner.run(request(timeframe), input_bars)

    assert first == repeated
    assert first.status is StudyStatus.AVAILABLE
    assert first.timeframe == timeframe
    assert first.data_snapshot_id == compute_bar_snapshot_id(input_bars.bars)
    assert store.find_cached_study_result(cache_key(first)) == first
    assert len(study.calls) == 1


def test_timeframe_config_and_snapshot_change_cache_identity(tmp_path: Path) -> None:
    runner, _store = setup(tmp_path)
    six_rows = PLUS_UP + ((14.0, 12.0, 13.0), (15.0, 13.0, 14.0))
    first = runner.run(request(), validated(bars(six_rows)))
    other_period = runner.run(
        request(config={"adx_period": 3}),
        validated(bars(six_rows)),
    )
    other_timeframe = runner.run(request("M15"), validated(bars(six_rows, "M15")))
    changed_rows = (*six_rows[:5], (15.0, 11.0, 12.0))
    other_snapshot = runner.run(request(), validated(bars(changed_rows)))

    assert first.data_snapshot_id == other_period.data_snapshot_id
    assert cache_key(first) != cache_key(other_period)
    assert first.timeframe != other_timeframe.timeframe
    assert first.data_snapshot_id != other_snapshot.data_snapshot_id
    assert (
        len(
            {
                cache_key(first),
                cache_key(other_period),
                cache_key(other_timeframe),
                cache_key(other_snapshot),
            }
        )
        == 4
    )


def test_all_timeframes_cache_and_sqlite_round_trip(tmp_path: Path) -> None:
    study = CountingADX()
    runner, store = setup(tmp_path, study)
    results: list[StudyResult] = []
    for timeframe in TIMEFRAMES:
        input_bars = validated(bars(PLUS_UP, timeframe))
        result = runner.run(request(timeframe), input_bars)
        assert runner.run(request(timeframe), input_bars) == result
        assert store.find_cached_study_result(cache_key(result)) == result
        results.append(result)

    assert len(study.calls) == 6
    assert len({result.data_snapshot_id for result in results}) == 6
    assert len({cache_key(result) for result in results}) == 6
    with sqlite3.connect(tmp_path / "adx.db") as connection:
        rows = connection.execute(
            "SELECT timeframe, state, value, normalized_value, direction, inputs_json, "
            "evidence_json, limitations_json FROM study_results ORDER BY id"
        ).fetchall()
    assert len(rows) == 6
    for row, result in zip(rows, results, strict=True):
        assert row[:5] == (
            result.timeframe,
            result.state,
            result.value,
            result.normalized_value,
            result.direction.value,
        )
        assert json.loads(row[5]) == {"adx_period": 2}
        assert json.loads(row[6]) == list(result.evidence)
        assert json.loads(row[7]) == list(result.limitations)


def test_f4_harness_future_append_and_mutation_do_not_change_results() -> None:
    report = assert_no_lookahead(
        blind(ADXDMIStudy),
        build_universe(18, "M5"),
        "M5",
        (end_of(5), end_of(10), end_of(15)),
        config=CONFIG,
    )
    assert report.cutoffs_checked == 3
    assert report.distinct_snapshots >= 2


@pytest.mark.parametrize(
    "status",
    [DataStatus.UNAVAILABLE, DataStatus.STALE, DataStatus.INSUFFICIENT_DATA, DataStatus.ERROR],
)
def test_coordinator_preserves_explicit_data_failures_without_computation(
    tmp_path: Path, status: DataStatus
) -> None:
    study = CountingADX()
    runner, _store = setup(tmp_path, study)
    failure = DataFailure(
        "SYNTHETIC_GOLD",
        "M5",
        CUTOFF,
        DataQualityReport(status, CUTOFF, ("SYNTHETIC_FAILURE",)),
    )
    analysis_request = MultiTimeframeAnalysisRequest(
        "SYNTHETIC_GOLD",
        CUTOFF,
        (StudyExecutionSpec(study.study_id, study.version, "M5", CONFIG),),
        {"M5": failure},
        FreshnessPolicy.one_timeframe_duration(),
    )

    result = MultiTimeframeAnalysisCoordinator(runner).run(analysis_request).components[0].result

    assert result.status is StudyStatus(status.value)
    assert result.data_snapshot_id is None
    assert result.evidence == (f"DATA_{status.value}", "SYNTHETIC_FAILURE")
    assert_no_values(result)
    assert study.calls == []


def test_coordinator_stale_input_is_not_computed(tmp_path: Path) -> None:
    study = CountingADX()
    runner, _store = setup(tmp_path, study)
    old_bars = bars(PLUS_UP, cutoff=CUTOFF - timedelta(minutes=6))
    input_bars = validated(old_bars)
    analysis_request = MultiTimeframeAnalysisRequest(
        "SYNTHETIC_GOLD",
        CUTOFF,
        (StudyExecutionSpec(study.study_id, study.version, "M5", CONFIG),),
        {"M5": input_bars},
        FreshnessPolicy.one_timeframe_duration(),
    )

    result = MultiTimeframeAnalysisCoordinator(runner).run(analysis_request).components[0].result

    assert result.status is StudyStatus.STALE
    assert result.data_snapshot_id is None
    assert result.evidence == ("DATA_STALE", "FRESHNESS_MAX_AGE_EXCEEDED")
    assert_no_values(result)
    assert study.calls == []
