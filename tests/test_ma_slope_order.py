"""Synthetic SMA ordering/slope unit and runner integration tests."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
from kankalar_gold_ai.studies.base import ConfigValue
from kankalar_gold_ai.studies.ma_slope_order import MASlopeOrderStudy

CUTOFF = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1")
CONFIG: dict[str, ConfigValue] = {
    "fast_period": 2,
    "middle_period": 3,
    "slow_period": 4,
    "slope_lookback": 1,
    "flat_slope_fraction_per_bar": 0.0,
}


def bars(
    closes: tuple[float, ...], timeframe: str = "M5", *, cutoff: datetime = CUTOFF
) -> tuple[MarketBar, ...]:
    duration = TIMEFRAME_DURATIONS[timeframe]
    return tuple(
        MarketBar(
            instrument="SYNTHETIC_GOLD",
            timeframe=timeframe,
            timestamp_utc=cutoff - duration * (len(closes) - index),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=None,
            source="synthetic-ma",
        )
        for index, close in enumerate(closes)
    )


def validated(items: tuple[MarketBar, ...], *, cutoff: datetime = CUTOFF) -> ValidatedBarSet:
    return ValidatedBarSet.from_bars(items, items[0].timeframe, cutoff, cutoff_utc=cutoff)


def run_request(
    timeframe: str = "M5",
    *,
    config: Mapping[str, ConfigValue] = CONFIG,
    cutoff: datetime = CUTOFF,
) -> StudyRunRequest:
    return StudyRunRequest(
        "trend.ma_slope_order", "0.1.0", "SYNTHETIC_GOLD", timeframe, cutoff, config
    )


class CountingStudy(MASlopeOrderStudy):
    def __init__(self) -> None:
        self.calls = 0

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        self.calls += 1
        return super().compute(bars, config)


def setup(
    tmp_path: Path, study: MASlopeOrderStudy | None = None, *, database: str = "study.db"
) -> tuple[StudyRunner, SQLitePersistence]:
    registry = StudyRegistry()
    registry.register(study or MASlopeOrderStudy())
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
    )


def assert_unavailable_fields(result: StudyResult) -> None:
    assert result.state is None
    assert result.value is None and result.normalized_value is None and result.uncertainty is None
    assert result.direction is Direction.NOT_APPLICABLE


@pytest.mark.parametrize(
    ("closes", "state", "raw", "normalized", "direction"),
    [
        ((1.0, 2.0, 3.0, 4.0, 5.0), "ORDER_UP_SLOPE_UP", 1.0, 2 / 7, Direction.BULLISH),
        ((5.0, 4.0, 3.0, 2.0, 1.0), "ORDER_DOWN_SLOPE_DOWN", -1.0, -0.4, Direction.BEARISH),
        ((1.0, 2.0, 4.0, 6.0, 3.0), "ORDER_UP_SLOPE_DOWN", -0.5, -0.1, Direction.NEUTRAL),
        ((1.0, 1.0, 1.0, 2.0, 1.0), "ORDER_UP_SLOPE_FLAT", 0.0, 0.0, Direction.NEUTRAL),
    ],
)
def test_known_numerical_examples(
    closes: tuple[float, ...], state: str, raw: float, normalized: float, direction: Direction
) -> None:
    study = MASlopeOrderStudy()
    result = study.compute(bars(closes), CONFIG)
    assert result.status is StudyStatus.AVAILABLE
    assert result.state == state
    assert result.value == raw and result.normalized_value == normalized
    assert result.direction is direction and result.uncertainty is None
    assert result.evidence and result.limitations
    assert study.compute(bars(closes), CONFIG) == result


@pytest.mark.parametrize(
    ("closes", "ordering", "slope"),
    [
        ((1, 1, 1, 1, 2), "UP", "UP"),
        ((1, 1, 2, 4, 1), "UP", "DOWN"),
        ((1, 1, 1, 2, 1), "UP", "FLAT"),
        ((1, 3, 3, 1, 4), "DOWN", "UP"),
        ((1, 2, 2, 1, 1), "DOWN", "DOWN"),
        ((1, 2, 2, 1, 2), "DOWN", "FLAT"),
        ((1, 1, 2, 1, 3), "MIXED", "UP"),
        ((1, 1, 2, 1, 1), "MIXED", "DOWN"),
        ((1, 1, 1, 1, 1), "MIXED", "FLAT"),
    ],
)
def test_all_nine_states(closes: tuple[int, ...], ordering: str, slope: str) -> None:
    result = MASlopeOrderStudy().compute(bars(tuple(float(c) for c in closes)), CONFIG)
    assert result.state == f"ORDER_{ordering}_SLOPE_{slope}"
    expected = (
        Direction.BULLISH
        if (ordering, slope) == ("UP", "UP")
        else Direction.BEARISH
        if (ordering, slope) == ("DOWN", "DOWN")
        else Direction.NEUTRAL
    )
    assert result.direction is expected


@pytest.mark.parametrize(
    "closes",
    [
        (1.0, 1.0, 1.0, 1.0),
        (1.0, 2.0, 4.0, 4.0),
        (1.0, 2.0, 1.0, 3.0),
        (1.0, 1.0, 3.0, 2.0),
    ],
)
def test_order_equalities_are_mixed(closes: tuple[float, ...]) -> None:
    config = {**CONFIG, "fast_period": 1, "middle_period": 2, "slow_period": 3}
    assert MASlopeOrderStudy().compute(bars(closes), config).state.startswith("ORDER_MIXED_")


@pytest.mark.parametrize("sign", [1, -1])
def test_slope_threshold_boundary_and_adjacent_floats(sign: int) -> None:
    closes = (1.0, 1.0, 1.0, 2.0) if sign == 1 else (2.0, 2.0, 2.0, 1.0)
    config = {**CONFIG, "fast_period": 1, "middle_period": 2, "slow_period": 3}
    study = MASlopeOrderStudy()
    value = study.compute(bars(closes), config).normalized_value
    assert value is not None
    threshold = abs(value)
    direction = "UP" if sign == 1 else "DOWN"
    for epsilon, expected in (
        (math.nextafter(threshold, 0.0), direction),
        (threshold, "FLAT"),
        (math.nextafter(threshold, math.inf), "FLAT"),
        (0.0, direction),
    ):
        output = study.compute(bars(closes), {**config, "flat_slope_fraction_per_bar": epsilon})
        assert output.state.endswith(f"_SLOPE_{expected}")


@pytest.mark.parametrize("field", ["fast_period", "middle_period", "slow_period", "slope_lookback"])
@pytest.mark.parametrize("bad", [True, 1.0, 0, -1, "2", None])
def test_invalid_period_values(field: str, bad: ConfigValue) -> None:
    with pytest.raises(StudyConfigError):
        MASlopeOrderStudy().validate_config({**CONFIG, field: bad})


@pytest.mark.parametrize(
    "bad", [True, -0.1, float("inf"), float("-inf"), float("nan"), "0", None, 10**400]
)
def test_invalid_thresholds(bad: ConfigValue) -> None:
    with pytest.raises(StudyConfigError):
        MASlopeOrderStudy().validate_config({**CONFIG, "flat_slope_fraction_per_bar": bad})


def test_missing_extra_and_unordered_configuration() -> None:
    study = MASlopeOrderStudy()
    for key in CONFIG:
        with pytest.raises(StudyConfigError):
            study.validate_config({k: v for k, v in CONFIG.items() if k != key})
    for config in (
        {**CONFIG, "other": 1},
        {**CONFIG, "fast_period": 3},
        {**CONFIG, "middle_period": 4},
    ):
        with pytest.raises(StudyConfigError):
            study.validate_config(config)
    assert study.validate_config({**CONFIG, "flat_slope_fraction_per_bar": 0}) == CONFIG


def test_warmup_and_lookback_larger_than_slow_period(tmp_path: Path) -> None:
    study = CountingStudy()
    runner, _ = setup(tmp_path, study)
    config = {**CONFIG, "slope_lookback": 4}
    assert study.min_bars(config) == 6
    short = validated(bars((1.0, 2.0, 3.0, 4.0, 5.0)))
    insufficient = runner.run(run_request(config=config), short)
    assert insufficient.status is StudyStatus.INSUFFICIENT_DATA
    assert insufficient.data_snapshot_id == compute_bar_snapshot_id(short.bars)
    assert_unavailable_fields(insufficient)
    assert study.calls == 0
    exact = validated(bars((1.0, 2.0, 3.0, 4.0, 5.0, 6.0)))
    result = runner.run(run_request(config=config), exact)
    assert result.status is StudyStatus.AVAILABLE and result.value == 1.0
    assert result.normalized_value == (4.0 / 1.5) / 4
    assert study.calls == 1


def test_subnormal_denominator_and_arithmetic_failures(tmp_path: Path) -> None:
    runner, _ = setup(tmp_path)
    tiny = math.nextafter(0.0, 1.0)
    config = {**CONFIG, "fast_period": 1, "middle_period": 2, "slow_period": 3}
    subnormal = validated(bars((tiny, tiny, 2 * tiny)))
    result = runner.run(run_request(config=config), subnormal)
    assert result.status is StudyStatus.AVAILABLE
    assert result.value == tiny and result.normalized_value == 1.0
    for prices in ((1.0, 1.2e308, 1.2e308), (tiny, tiny, 1.0)):
        input_data = validated(bars(prices))
        failed = runner.run(run_request(config=config), input_data)
        assert failed.status is StudyStatus.ERROR
        assert failed.evidence == ("STUDY_EXECUTION_ERROR",)
        assert failed.data_snapshot_id == compute_bar_snapshot_id(input_data.bars)
        assert_unavailable_fields(failed)
    with pytest.raises(ArithmeticError):
        MASlopeOrderStudy().compute(bars((0.0, 0.0, 1.0)), config)


def test_invalid_ohlc_and_unknown_gap_fail_closed() -> None:
    items = bars((1.0, 2.0, 3.0, 4.0))
    with pytest.raises(HistoricalDataQualityError) as empty:
        ValidatedBarSet.from_bars((), "M5", CUTOFF, cutoff_utc=CUTOFF)
    assert empty.value.report.status is DataStatus.INSUFFICIENT_DATA
    invalid = (*items[:-1], replace(items[-1], high=float("nan")))
    with pytest.raises(HistoricalDataQualityError):
        validated(invalid)
    with pytest.raises(HistoricalDataQualityError) as caught:
        validated((items[0], *items[2:]))
    assert "GAP_SESSION_UNKNOWN" in caught.value.report.reasons


def test_accepted_closure_counts_observed_bars() -> None:
    items = bars((1.0, 2.0, 3.0, 4.0, 5.0))
    gapped = (items[0], *items[2:])
    accepted = ValidatedBarSet.from_bars(
        gapped, "M5", CUTOFF, cutoff_utc=CUTOFF, expected_open=lambda _: False
    )
    contiguous = bars((1.0, 3.0, 4.0, 5.0))
    assert MASlopeOrderStudy().compute(accepted.bars, CONFIG) == MASlopeOrderStudy().compute(
        contiguous, CONFIG
    )


@pytest.mark.parametrize(
    "status",
    [DataStatus.STALE, DataStatus.UNAVAILABLE, DataStatus.INSUFFICIENT_DATA, DataStatus.ERROR],
)
def test_explicit_data_failures(tmp_path: Path, status: DataStatus) -> None:
    study = CountingStudy()
    runner, _ = setup(tmp_path, study)
    failure = DataFailure(
        "SYNTHETIC_GOLD", "M5", CUTOFF, DataQualityReport(status, CUTOFF, ("TEST_FAILURE",))
    )
    result = runner.run_data_failure(run_request(), failure)
    assert result.status is StudyStatus(status.value)
    assert result.evidence == (f"DATA_{status.value}", "TEST_FAILURE")
    assert result.data_snapshot_id is None and study.calls == 0
    assert_unavailable_fields(result)


def test_coordinator_stale_data_is_not_computed(tmp_path: Path) -> None:
    study = CountingStudy()
    runner, _ = setup(tmp_path, study)
    old = bars((1.0, 2.0, 3.0, 4.0), cutoff=CUTOFF - timedelta(minutes=6))
    input_data = validated(old)
    request = MultiTimeframeAnalysisRequest(
        "SYNTHETIC_GOLD",
        CUTOFF,
        (StudyExecutionSpec(study.study_id, study.version, "M5", CONFIG),),
        {"M5": input_data},
        FreshnessPolicy.one_timeframe_duration(),
    )
    result = MultiTimeframeAnalysisCoordinator(runner).run(request).components[0].result
    assert result.status is StudyStatus.STALE and result.data_snapshot_id is None
    assert result.evidence == ("DATA_STALE", "FRESHNESS_MAX_AGE_EXCEEDED")
    assert_unavailable_fields(result)
    assert study.calls == 0


def test_all_timeframes_cache_and_sqlite_round_trip(tmp_path: Path) -> None:
    study = CountingStudy()
    runner, store = setup(tmp_path, study)
    assert study.required_timeframes == frozenset(TIMEFRAMES)
    results = []
    for timeframe in TIMEFRAMES:
        input_data = validated(bars((1.0, 2.0, 3.0, 4.0, 5.0), timeframe))
        result = runner.run(run_request(timeframe), input_data)
        assert result.status is StudyStatus.AVAILABLE and result.timeframe == timeframe
        assert result.data_snapshot_id == compute_bar_snapshot_id(input_data.bars)
        assert runner.run(run_request(timeframe), input_data) == result
        assert store.find_cached_study_result(cache_key(result)) == result
        results.append(result)
    assert study.calls == 6 and len({cache_key(result) for result in results}) == 6
    with sqlite3.connect(tmp_path / "study.db") as connection:
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
        assert json.loads(row[5]) == result.inputs
        assert json.loads(row[6]) == list(result.evidence)
        assert json.loads(row[7]) == list(result.limitations)


def test_config_and_snapshot_change_cache_identity(tmp_path: Path) -> None:
    study = CountingStudy()
    runner, _ = setup(tmp_path, study)
    first = runner.run(run_request(), validated(bars((1.0, 2.0, 3.0, 4.0, 5.0))))
    other_config = runner.run(
        run_request(config={**CONFIG, "flat_slope_fraction_per_bar": 0.5}),
        validated(bars((1.0, 2.0, 3.0, 4.0, 5.0))),
    )
    other_bars = runner.run(run_request(), validated(bars((2.0, 2.0, 3.0, 4.0, 5.0))))
    assert len({cache_key(first), cache_key(other_config), cache_key(other_bars)}) == 3
    assert first.value == other_bars.value and first.normalized_value == other_bars.normalized_value
    assert first.data_snapshot_id != other_bars.data_snapshot_id and study.calls == 3


def test_no_lookahead_and_older_unused_input(tmp_path: Path) -> None:
    study = CountingStudy()
    history = bars((1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0))
    cutoff = history[4].timestamp_utc + TIMEFRAME_DURATIONS["M5"]
    prefix = tuple(
        bar for bar in history if bar.timestamp_utc + TIMEFRAME_DURATIONS["M5"] <= cutoff
    )
    future_mutated = (
        *history[:5],
        *(replace(bar, open=100.0, high=100.0, low=100.0, close=100.0) for bar in history[5:]),
    )
    outputs = []
    for index, universe in enumerate(
        (
            history,
            future_mutated,
            (
                *history,
                replace(
                    history[-1], timestamp_utc=history[-1].timestamp_utc + TIMEFRAME_DURATIONS["M5"]
                ),
            ),
        )
    ):
        selected = tuple(
            bar for bar in universe if bar.timestamp_utc + TIMEFRAME_DURATIONS["M5"] <= cutoff
        )
        assert selected == prefix
        runner, _ = setup(tmp_path, study, database=f"lookahead-{index}.db")
        result = runner.run(run_request(cutoff=cutoff), validated(selected, cutoff=cutoff))
        outputs.append(result)
    assert outputs[0] == outputs[1] == outputs[2]
    assert study.calls == 3
    with pytest.raises(ValueError, match="complete after cutoff"):
        ValidatedBarSet.from_bars(history, "M5", CUTOFF, cutoff_utc=cutoff)
