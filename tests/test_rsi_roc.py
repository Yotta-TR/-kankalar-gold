"""Synthetic, independently calculated momentum examples and integration checks."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
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
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.data.snapshot import compute_bar_snapshot_id
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import SQLitePersistence
from kankalar_gold_ai.studies import (
    DataFailure,
    StudyConfigError,
    StudyRegistry,
    StudyRequestError,
    StudyRunner,
    StudyRunRequest,
    run_pipeline_study,
)
from kankalar_gold_ai.studies.base import BarStudy, ConfigValue
from kankalar_gold_ai.studies.roc_momentum import ROCMomentumStudy
from kankalar_gold_ai.studies.rsi import RSIStudy
from tests.fixtures.point_in_time import (
    DURATIONS,
    assert_no_lookahead,
    blind,
    build_universe,
    bump_last_selected_close,
    end_of,
)

CUTOFF = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
CASES = [(RSIStudy, "rsi_period", 2), (ROCMomentumStudy, "roc_period", 2)]


def bars(closes: tuple[float, ...], timeframe: str = "M5") -> tuple[MarketBar, ...]:
    duration = DURATIONS[timeframe]
    return tuple(
        MarketBar(
            "TEST_GOLD",
            timeframe,
            CUTOFF - duration * (len(closes) - index),
            close,
            close,
            close,
            close,
            None,
            "synthetic",
        )
        for index, close in enumerate(closes)
    )


def setup(tmp_path: Path, study: BarStudy) -> tuple[StudyRunner, SQLitePersistence]:
    registry = StudyRegistry()
    registry.register(study)
    store = SQLitePersistence(tmp_path / "momentum.db")
    store.initialize()
    return StudyRunner(registry, store), store


def run(
    runner: StudyRunner, study: BarStudy, items: tuple[MarketBar, ...], period: int
) -> StudyResult:
    config = {"rsi_period" if study.study_id == "trend.rsi" else "roc_period": period}
    request = StudyRunRequest(
        study.study_id, study.version, "TEST_GOLD", items[0].timeframe, CUTOFF, config
    )
    input_bars = ValidatedBarSet.from_bars(items, items[0].timeframe, CUTOFF, cutoff_utc=CUTOFF)
    return runner.run(request, input_bars)


@pytest.mark.parametrize(
    ("closes", "level", "movement", "state", "direction"),
    [
        (
            (10.0, 12.0, 11.0, 14.0),
            800 / 9,
            (800 / 9 - 200 / 3) / 100,
            "RSI_RISING",
            Direction.BULLISH,
        ),
        (
            (10.0, 12.0, 11.0, 9.0),
            200 / 7,
            (200 / 7 - 200 / 3) / 100,
            "RSI_FALLING",
            Direction.BEARISH,
        ),
        ((10.0, 10.0, 10.0, 10.0), 50.0, 0.0, "RSI_FLAT", Direction.NEUTRAL),
        ((10.0, 11.0, 12.0, 13.0), 100.0, 0.0, "RSI_FLAT", Direction.NEUTRAL),
        ((13.0, 12.0, 11.0, 10.0), 0.0, 0.0, "RSI_FLAT", Direction.NEUTRAL),
    ],
)
def test_rsi_hand_calculated(
    closes: tuple[float, ...], level: float, movement: float, state: str, direction: Direction
) -> None:
    output = RSIStudy().compute(bars(closes), {"rsi_period": 2})
    assert output.value == pytest.approx(level)
    assert output.normalized_value == pytest.approx(movement)
    assert output.state == state
    assert output.direction is direction
    assert output.evidence and output.limitations


@pytest.mark.parametrize(
    ("closes", "value", "state", "direction"),
    [
        ((100.0, 110.0, 125.0), 25.0, "POSITIVE", Direction.BULLISH),
        ((100.0, 90.0, 75.0), -25.0, "NEGATIVE", Direction.BEARISH),
        ((100.0, 90.0, 100.0), 0.0, "ZERO", Direction.NEUTRAL),
    ],
)
def test_roc_hand_calculated(
    closes: tuple[float, ...], value: float, state: str, direction: Direction
) -> None:
    output = ROCMomentumStudy().compute(bars(closes), {"roc_period": 2})
    assert output.value == pytest.approx(value)
    assert output.state == state and output.direction is direction
    assert output.normalized_value is None
    assert output.evidence and output.limitations


@pytest.mark.parametrize(("factory", "key", "period"), CASES)
@pytest.mark.parametrize("bad", [True, 1.0, 0, -1, "2", None])
def test_invalid_config(
    factory: type[RSIStudy] | type[ROCMomentumStudy], key: str, period: int, bad: ConfigValue
) -> None:
    study = factory()
    for config in ({key: bad}, {}, {key: period, "extra": 1}):
        with pytest.raises(StudyConfigError):
            study.validate_config(config)


def test_rsi_minimum_period() -> None:
    with pytest.raises(StudyConfigError):
        RSIStudy().validate_config({"rsi_period": 1})


@pytest.mark.parametrize(("factory", "key", "period"), CASES)
def test_warmup_and_failed_input(
    tmp_path: Path, factory: type[RSIStudy] | type[ROCMomentumStudy], key: str, period: int
) -> None:
    study = factory()
    runner, _ = setup(tmp_path, study)
    minimum = study.min_bars({key: period})
    short = bars(tuple(10.0 + i for i in range(minimum - 1)))
    result = run(runner, study, short, period)
    assert result.status is StudyStatus.INSUFFICIENT_DATA
    assert result.value is None and result.normalized_value is None
    assert (
        run(runner, study, bars(tuple(10.0 + i for i in range(minimum))), period).status
        is StudyStatus.AVAILABLE
    )
    request = StudyRunRequest(
        study.study_id, study.version, "TEST_GOLD", "M5", CUTOFF, {key: period}
    )
    failure = DataFailure(
        "TEST_GOLD", "M5", CUTOFF, DataQualityReport(DataStatus.ERROR, CUTOFF, ("BAD_DATA",))
    )
    failed = runner.run_data_failure(request, failure)
    assert failed.status is StudyStatus.ERROR and failed.value is None
    with pytest.raises(StudyRequestError):
        runner.run(request, short)


@pytest.mark.parametrize(("factory", "key", "period"), CASES)
@pytest.mark.parametrize("timeframe", tuple(DURATIONS))
def test_timeframe_cache_and_round_trip(
    tmp_path: Path,
    factory: type[RSIStudy] | type[ROCMomentumStudy],
    key: str,
    period: int,
    timeframe: str,
) -> None:
    study = factory()
    runner, store = setup(tmp_path, study)
    items = bars((10.0, 12.0, 11.0, 14.0, 13.0), timeframe)
    result = run(runner, study, items, period)
    assert result.status is StudyStatus.AVAILABLE
    assert result == run(runner, study, items, period)
    assert result.data_snapshot_id == compute_bar_snapshot_id(items)
    cache_key = StudyResultKey(
        result.study_id,
        result.version,
        result.instrument,
        timeframe,
        CUTOFF,
        canonical_inputs_json(result.inputs),
        result.data_snapshot_id,
    )
    assert store.find_cached_study_result(cache_key) == result
    other = run(runner, study, items, 3)
    assert result.inputs != other.inputs and result.data_snapshot_id == other.data_snapshot_id
    assert other.value != result.value
    other_key = StudyResultKey(
        other.study_id,
        other.version,
        other.instrument,
        timeframe,
        CUTOFF,
        canonical_inputs_json(other.inputs),
        other.data_snapshot_id,
    )
    reopened = SQLitePersistence(tmp_path / "momentum.db")
    assert reopened.find_cached_study_result(other_key) == other
    changed = run(runner, study, bars((10.0, 12.0, 11.0, 14.0, 15.0), timeframe), period)
    assert changed.data_snapshot_id != result.data_snapshot_id
    assert changed.value != result.value


@pytest.mark.parametrize(("factory", "key", "period"), CASES)
def test_pipeline_integration(
    tmp_path: Path, factory: type[RSIStudy] | type[ROCMomentumStudy], key: str, period: int
) -> None:
    study = factory()
    runner, _ = setup(tmp_path, study)
    items = bars((10.0, 12.0, 11.0, 14.0))

    class Provider:
        def get_bars(
            self, instrument: str, timeframe: str, start_utc: datetime, end_utc: datetime
        ) -> tuple[MarketBar, ...]:
            return items

    pipeline = HistoricalDataPipeline(Provider(), clock=lambda: CUTOFF)
    request = StudyRunRequest(
        study.study_id, study.version, "TEST_GOLD", "M5", CUTOFF, {key: period}
    )
    result = run_pipeline_study(runner, pipeline, request, history_days=1)
    assert result.status is StudyStatus.AVAILABLE
    assert result.data_snapshot_id == compute_bar_snapshot_id(items)


@pytest.mark.parametrize(("factory", "key", "period"), CASES)
def test_no_lookahead(
    factory: type[RSIStudy] | type[ROCMomentumStudy], key: str, period: int
) -> None:
    report = assert_no_lookahead(
        blind(factory),
        build_universe(18),
        "M5",
        (end_of(5), end_of(10), end_of(15)),
        config={key: period},
        sensitive_mutation=bump_last_selected_close("M5"),
    )
    assert report.cutoffs_checked == 3


@pytest.mark.parametrize(("factory", "key", "period"), CASES)
@pytest.mark.parametrize("invalid", [0.0, -1.0, math.nan, math.inf])
def test_invalid_close_fails(
    factory: type[RSIStudy] | type[ROCMomentumStudy], key: str, period: int, invalid: float
) -> None:
    with pytest.raises(ArithmeticError):
        factory().compute(bars((10.0, 11.0, 12.0, invalid)), {key: period})


def test_overflow_fails_closed(tmp_path: Path) -> None:
    study = ROCMomentumStudy()
    runner, _ = setup(tmp_path, study)
    result = run(runner, study, bars((1e-300, 1e300)), 1)
    assert result.status is StudyStatus.ERROR
    assert result.value is None and result.direction is Direction.NOT_APPLICABLE
    with pytest.raises(OverflowError):
        RSIStudy().compute(bars((1.0, 1e308, 1.0, 1e308, 1.0, 1e308)), {"rsi_period": 4})


def test_rsi_overflow_is_persisted_by_runner(tmp_path: Path) -> None:
    study = RSIStudy()
    runner, _ = setup(tmp_path, study)
    result = run(runner, study, bars((1.0, 1e308, 1.0, 1e308, 1.0, 1e308)), 4)
    assert result.status is StudyStatus.ERROR
    assert result.value is None and result.normalized_value is None and result.uncertainty is None
    assert result.direction is Direction.NOT_APPLICABLE
    with sqlite3.connect(tmp_path / "momentum.db") as connection:
        row = connection.execute(
            "SELECT status, value, normalized_value, uncertainty, direction FROM study_results"
        ).fetchone()
    assert row == ("ERROR", None, None, None, "NOT_APPLICABLE")
