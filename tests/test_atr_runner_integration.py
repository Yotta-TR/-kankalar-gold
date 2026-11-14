"""Runner, point-in-time, cache and SQLite integration tests for ATR."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

import pytest

from kankalar_gold_ai.core.contracts import (
    Direction,
    MarketBar,
    StudyResult,
    StudyStatus,
)
from kankalar_gold_ai.core.study_results import (
    StudyResultKey,
    canonical_inputs_json,
)
from kankalar_gold_ai.data.quality import TIMEFRAME_DURATIONS
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import SQLitePersistence
from kankalar_gold_ai.studies import (
    StudyConfigError,
    StudyOutput,
    StudyRegistry,
    StudyRunner,
    StudyRunRequest,
)
from kankalar_gold_ai.studies.atr import ATRStudy
from kankalar_gold_ai.studies.base import ConfigValue

START = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
COMMON_CUTOFF = START + timedelta(days=10)
SOURCE = "synthetic-atr-fixture"
SUPPORTED_TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1")
DURATION = TIMEFRAME_DURATIONS["M15"]


def _bars(
    count: int = 8,
    *,
    timeframe: str = "M15",
    offset: float = 0.0,
) -> tuple[MarketBar, ...]:
    duration = TIMEFRAME_DURATIONS[timeframe]
    return tuple(
        MarketBar(
            instrument="TEST_GOLD",
            timeframe=timeframe,
            timestamp_utc=START + duration * index,
            open=100.0 + offset,
            high=101.0 + index + offset,
            low=99.0 + offset,
            close=100.0 + offset,
            volume=None,
            source=SOURCE,
        )
        for index in range(count)
    )


def _validated(
    bars: tuple[MarketBar, ...],
    *,
    cutoff: datetime | None = None,
) -> ValidatedBarSet:
    end = cutoff or bars[-1].timestamp_utc + TIMEFRAME_DURATIONS[bars[0].timeframe]
    return ValidatedBarSet.from_bars(bars, bars[0].timeframe, end, cutoff_utc=end)


def _request(
    period: int = 2,
    *,
    timeframe: str = "M15",
    as_of: datetime | None = None,
) -> StudyRunRequest:
    return StudyRunRequest(
        study_id="volatility.atr",
        version="0.1.0",
        instrument="TEST_GOLD",
        timeframe=timeframe,
        as_of_utc=as_of or START + TIMEFRAME_DURATIONS[timeframe] * 8,
        config={"atr_period": period},
    )


class _CountingATR(ATRStudy):
    def __init__(self) -> None:
        self.compute_calls = 0
        self.seen_bars: list[tuple[MarketBar, ...]] = []

    def compute(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, ConfigValue],
    ) -> StudyOutput:
        self.compute_calls += 1
        self.seen_bars.append(bars)
        return super().compute(bars, config)


def _runner(
    tmp_path: Path,
    study: ATRStudy | None = None,
    *,
    database: str = "atr.db",
) -> tuple[StudyRunner, SQLitePersistence]:
    registry = StudyRegistry()
    registry.register(study or ATRStudy())
    store = SQLitePersistence(tmp_path / database)
    store.initialize()
    return StudyRunner(registry, store), store


def test_n_bars_are_insufficient_and_n_plus_one_compute(tmp_path: Path) -> None:
    study = _CountingATR()
    runner, _store = _runner(tmp_path, study)
    request = _request(period=3)
    cutoff = START + DURATION * 8

    insufficient = runner.run(request, _validated(_bars(3), cutoff=cutoff))
    assert insufficient.status is StudyStatus.INSUFFICIENT_DATA
    assert insufficient.value is None
    assert study.compute_calls == 0

    available = runner.run(request, _validated(_bars(4), cutoff=cutoff))
    assert available.status is StudyStatus.AVAILABLE
    assert study.compute_calls == 1


@pytest.mark.parametrize(
    ("timeframe", "duration"),
    tuple((timeframe, TIMEFRAME_DURATIONS[timeframe]) for timeframe in SUPPORTED_TIMEFRAMES),
)
def test_atr_runs_independently_on_every_supported_timeframe(
    tmp_path: Path,
    timeframe: str,
    duration: timedelta,
) -> None:
    study = _CountingATR()
    runner, _store = _runner(tmp_path, study)
    bars = _bars(5, timeframe=timeframe)
    validated = _validated(bars, cutoff=COMMON_CUTOFF)
    request = _request(timeframe=timeframe, as_of=COMMON_CUTOFF)

    result = runner.run(request, validated)

    assert result.timeframe == timeframe
    assert result.as_of_utc == COMMON_CUTOFF
    assert result.status is StudyStatus.AVAILABLE
    assert study.compute_calls == 1
    assert study.seen_bars == [validated.bars]
    assert all(bar.timeframe == timeframe for bar in study.seen_bars[0])
    assert study.seen_bars[0][-1].timestamp_utc + duration <= COMMON_CUTOFF


def test_one_atr_registration_has_independent_results_and_cache_keys_per_timeframe(
    tmp_path: Path,
) -> None:
    study = _CountingATR()
    runner, store = _runner(tmp_path, study)
    results: dict[str, StudyResult] = {}
    keys: dict[str, StudyResultKey] = {}

    for timeframe in SUPPORTED_TIMEFRAMES:
        validated = _validated(_bars(5, timeframe=timeframe), cutoff=COMMON_CUTOFF)
        request = _request(timeframe=timeframe, as_of=COMMON_CUTOFF)
        result = runner.run(request, validated)
        results[timeframe] = result
        keys[timeframe] = StudyResultKey(
            study_id=result.study_id,
            version=result.version,
            instrument=result.instrument,
            timeframe=result.timeframe,
            as_of_utc=result.as_of_utc,
            inputs_json=canonical_inputs_json(result.inputs),
            data_snapshot_id=result.data_snapshot_id or "",
        )

    assert study.required_timeframes == frozenset(SUPPORTED_TIMEFRAMES)
    assert study.compute_calls == len(SUPPORTED_TIMEFRAMES)
    assert set(results) == set(SUPPORTED_TIMEFRAMES)
    assert len({result.timeframe for result in results.values()}) == 6
    assert len(set(keys.values())) == 6
    assert len({result.data_snapshot_id for result in results.values()}) == 6

    for timeframe in SUPPORTED_TIMEFRAMES:
        assert results[timeframe].timeframe == timeframe
        assert store.find_cached_study_result(keys[timeframe]) == results[timeframe]
    assert study.compute_calls == len(SUPPORTED_TIMEFRAMES)
    assert len(study.seen_bars) == len(SUPPORTED_TIMEFRAMES)
    assert all(len({bar.timeframe for bar in bars}) == 1 for bars in study.seen_bars)
    assert {bars[0].timeframe for bars in study.seen_bars} == set(SUPPORTED_TIMEFRAMES)


def test_persistence_round_trip_and_period_cache_identity(tmp_path: Path) -> None:
    runner, store = _runner(tmp_path)
    bars = _validated(_bars())

    first = runner.run(_request(2), bars)
    repeated = runner.run(_request(2), bars)
    different_period = runner.run(_request(3), bars)

    assert first == repeated
    assert first.status is StudyStatus.AVAILABLE
    assert first.direction is Direction.NOT_APPLICABLE
    assert first.inputs == {"atr_period": 2}
    assert different_period.inputs == {"atr_period": 3}
    assert first.data_snapshot_id == different_period.data_snapshot_id

    first_key = StudyResultKey(
        study_id=first.study_id,
        version=first.version,
        instrument=first.instrument,
        timeframe=first.timeframe,
        as_of_utc=first.as_of_utc,
        inputs_json=canonical_inputs_json(first.inputs),
        data_snapshot_id=first.data_snapshot_id or "",
    )
    second_key = StudyResultKey(
        study_id=different_period.study_id,
        version=different_period.version,
        instrument=different_period.instrument,
        timeframe=different_period.timeframe,
        as_of_utc=different_period.as_of_utc,
        inputs_json=canonical_inputs_json(different_period.inputs),
        data_snapshot_id=different_period.data_snapshot_id or "",
    )
    assert first_key != second_key
    assert store.find_cached_study_result(first_key) == first
    assert store.find_cached_study_result(second_key) == different_period

    with sqlite3.connect(tmp_path / "atr.db") as connection:
        rows = connection.execute(
            "SELECT study_id, version, instrument, timeframe, status, state, value, "
            "normalized_value, direction, uncertainty, inputs_json, evidence_json, "
            "limitations_json FROM study_results ORDER BY id"
        ).fetchall()
    assert len(rows) == 2
    row = rows[0]
    assert row[:5] == (
        "volatility.atr",
        "0.1.0",
        "TEST_GOLD",
        "M15",
        "AVAILABLE",
    )
    assert row[5] is None
    assert row[6] == first.value
    assert row[7] is None
    assert row[8] == "NOT_APPLICABLE"
    assert row[9] is None
    assert json.loads(row[10]) == {"atr_period": 2}
    assert json.loads(row[11]) == list(first.evidence)
    assert json.loads(row[12]) == list(first.limitations)


def test_future_bars_cannot_change_earlier_cutoff_result(tmp_path: Path) -> None:
    universe = _bars(8)
    extended_universe = _bars(11)
    cutoff = START + DURATION * 4
    prefix = tuple(bar for bar in universe if bar.timestamp_utc + DURATION <= cutoff)
    altered_future = tuple(
        MarketBar(
            instrument=bar.instrument,
            timeframe=bar.timeframe,
            timestamp_utc=bar.timestamp_utc,
            open=bar.open + 500.0,
            high=bar.high + 500.0,
            low=bar.low + 500.0,
            close=bar.close + 500.0,
            volume=bar.volume,
            source=bar.source,
        )
        if bar.timestamp_utc + DURATION > cutoff
        else bar
        for bar in universe
    )
    altered_prefix = tuple(bar for bar in altered_future if bar.timestamp_utc + DURATION <= cutoff)
    appended_prefix = tuple(
        bar for bar in extended_universe if bar.timestamp_utc + DURATION <= cutoff
    )

    first_study = _CountingATR()
    altered_study = _CountingATR()
    appended_study = _CountingATR()
    first_runner, _first_store = _runner(tmp_path, first_study, database="base.db")
    second_runner, _second_store = _runner(tmp_path, altered_study, database="altered.db")
    appended_runner, _appended_store = _runner(tmp_path, appended_study, database="appended.db")
    first = first_runner.run(_request(as_of=cutoff), _validated(prefix, cutoff=cutoff))
    altered = second_runner.run(_request(as_of=cutoff), _validated(altered_prefix, cutoff=cutoff))
    appended = appended_runner.run(
        _request(as_of=cutoff), _validated(appended_prefix, cutoff=cutoff)
    )

    assert first == altered
    assert first == appended
    assert first_study.seen_bars == [prefix]
    assert altered_study.seen_bars == [altered_prefix]
    assert appended_study.seen_bars == [appended_prefix]


def test_arithmetic_overflow_uses_runner_execution_error_path(tmp_path: Path) -> None:
    runner, _store = _runner(tmp_path)
    bars = tuple(
        MarketBar(
            instrument="TEST_GOLD",
            timeframe="M15",
            timestamp_utc=START + DURATION * index,
            open=1.0,
            high=1.2e308,
            low=1.0,
            close=1.0,
            volume=None,
            source=SOURCE,
        )
        for index in range(3)
    )
    request = _request(period=2, as_of=START + DURATION * 3)

    result = runner.run(request, _validated(bars))

    assert result.status is StudyStatus.ERROR
    assert result.value is None
    assert result.evidence == ("STUDY_EXECUTION_ERROR",)


def test_invalid_period_is_rejected_by_runner(tmp_path: Path) -> None:
    runner, _store = _runner(tmp_path)
    request = StudyRunRequest(
        study_id="volatility.atr",
        version="0.1.0",
        instrument="TEST_GOLD",
        timeframe="M15",
        as_of_utc=START + DURATION * 8,
        config={"atr_period": True},
    )

    with pytest.raises(StudyConfigError):
        runner.run(request, _validated(_bars()))
