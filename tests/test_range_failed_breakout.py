"""Point-in-time tests for close-confirmed range failures."""

import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
from kankalar_gold_ai.core.contracts import (
    FailedBreakoutType,
    MarketBar,
    StructureFailedBreakoutEvent,
    StudyEventConflictError,
    StudyStatus,
)
from kankalar_gold_ai.data.calendar import CalendarProvenance, SessionCalendar, WeeklySession
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import MigrationError, SQLitePersistence
from kankalar_gold_ai.studies import (
    RangeFailedBreakoutStudy,
    StudyConfigError,
    StudyRegistry,
    StudyRunner,
    StudyRunRequest,
    run_pipeline_study,
)
from tests.fixtures.point_in_time import (
    INSTRUMENT,
    SOURCE,
    assert_no_lookahead,
    blind,
    end_of,
    run_at,
)
from tests.test_liquidity_sweep import bars as sweep_bars
from tests.test_liquidity_sweep import run as run_sweep

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
CONFIG = {"lookback_bars": 2, "reclaim_window_bars": 2, "breakout_buffer_bps": 100}


def bars(closes: tuple[float, ...]) -> tuple[MarketBar, ...]:
    return tuple(
        MarketBar(
            "GOLD",
            "M5",
            START + timedelta(minutes=5 * i),
            100,
            max(110, close),
            min(90, close),
            close,
            None,
            "test",
        )
        for i, close in enumerate(closes)
    )


def run(path: Path, raw: tuple[MarketBar, ...], config: dict = CONFIG):
    cutoff = raw[-1].timestamp_utc + timedelta(minutes=5)
    valid = ValidatedBarSet.from_bars(raw, "M5", cutoff, cutoff_utc=cutoff)
    store = SQLitePersistence(path)
    store.initialize()
    registry = StudyRegistry()
    registry.register(RangeFailedBreakoutStudy())
    runner = StudyRunner(registry, store)
    request = StudyRunRequest(
        "structure.range_failed_breakout", "0.1.0", "GOLD", "M5", cutoff, config
    )
    return runner.run(request, valid), store, runner, request, valid


@pytest.mark.parametrize(
    ("closes", "side", "expected"),
    [
        ((100, 100, 111.1, 100), FailedBreakoutType.UPSIDE, False),
        ((100, 100, 111.099999, 100), FailedBreakoutType.UPSIDE, False),
        ((100, 100, 111.100001, 110), FailedBreakoutType.UPSIDE, True),
        ((100, 100, 89.1, 100), FailedBreakoutType.DOWNSIDE, False),
        ((100, 100, 89.100001, 100), FailedBreakoutType.DOWNSIDE, False),
        ((100, 100, 89.099999, 90), FailedBreakoutType.DOWNSIDE, True),
    ],
)
def test_buffer_and_inclusive_reclaim(
    tmp_path: Path, closes: tuple[float, ...], side: FailedBreakoutType, expected: bool
) -> None:
    raw = bars(closes)
    result, *_ = run(tmp_path / "test.db", raw)
    assert bool(result.events) is expected
    if expected:
        event = result.events[0]
        assert isinstance(event, StructureFailedBreakoutEvent)
        assert event.event_type is side
        assert (event.range_low, event.range_high) == (90, 110)
        assert event.range_start_utc == raw[0].timestamp_utc
        assert event.range_end_utc == raw[1].timestamp_utc
        assert event.breakout_time_utc == raw[2].timestamp_utc
        assert event.reclaim_time_utc == raw[3].timestamp_utc
        assert event.known_at_utc == raw[3].timestamp_utc + timedelta(minutes=5)


@pytest.mark.parametrize(
    ("closes", "expected"),
    [
        ((100, 100, 112, 115, 100), 1),
        ((100, 100, 112, 115, 115, 100), 0),
        ((100, 100, 112, 80, 100), 0),
        ((100, 100, 88, 120, 100), 0),
        ((100, 100, 112, 115, 100, 120, 100), 2),
    ],
)
def test_window_invalidation_and_new_candidates(
    tmp_path: Path, closes: tuple[float, ...], expected: int
) -> None:
    result, *_ = run(tmp_path / "test.db", bars(closes))
    assert len(result.events) == expected


def test_wicks_zero_width_and_frozen_range(tmp_path: Path) -> None:
    wick = list(bars((100, 100, 100, 100)))
    wick[2] = replace(wick[2], high=120, low=80)
    assert not run(tmp_path / "wick.db", tuple(wick))[0].events
    flat = tuple(replace(bar, high=100, low=100) for bar in bars((100, 100, 112, 100))[:2])
    assert not run(tmp_path / "flat.db", flat + bars((100, 100, 112, 100))[2:])[0].events
    raw = bars((100, 100, 112, 115, 109))
    result, *_ = run(tmp_path / "frozen.db", raw)
    event = result.events[0]
    assert isinstance(event, StructureFailedBreakoutEvent)
    assert event.range_high == 110 and event.reclaim_time_utc == raw[4].timestamp_utc


@pytest.mark.parametrize(
    ("previous", "breakout", "reclaim", "side"),
    [
        (110, 110.000001, 90, FailedBreakoutType.UPSIDE),
        (90, 89.999999, 110, FailedBreakoutType.DOWNSIDE),
    ],
)
def test_zero_buffer_and_previous_close_at_boundary(
    tmp_path: Path, previous: float, breakout: float, reclaim: float, side: FailedBreakoutType
) -> None:
    config = {**CONFIG, "breakout_buffer_bps": 0}
    result, *_ = run(tmp_path / "edge.db", bars((100, previous, breakout, reclaim)), config)
    assert len(result.events) == 1
    assert result.events[0].event_type is side


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"lookback_bars": True, "reclaim_window_bars": 1, "breakout_buffer_bps": 0},
        {"lookback_bars": 1, "reclaim_window_bars": 0, "breakout_buffer_bps": 0},
        {"lookback_bars": 1, "reclaim_window_bars": 1, "breakout_buffer_bps": -1},
        {"lookback_bars": 1, "reclaim_window_bars": 1, "breakout_buffer_bps": float("inf")},
    ],
)
def test_bad_configuration(config: dict) -> None:
    with pytest.raises(StudyConfigError):
        RangeFailedBreakoutStudy().validate_config(config)


def test_cache_replay_pipeline_and_future_append(tmp_path: Path) -> None:
    raw = bars((100, 100, 112, 100, 115, 100))
    early, store, runner, request, valid = run(tmp_path / "replay.db", raw[:4])
    assert early.status is StudyStatus.AVAILABLE and len(early.events) == 1
    assert runner.run(request, valid) == early
    later, *_ = run(tmp_path / "replay.db", raw)
    assert early.events[0] in later.events
    assert runner.run(request, valid) == early
    pipeline = HistoricalDataPipeline(MockMarketDataProvider(raw), clock=lambda: valid.cutoff_utc)
    assert run_pipeline_study(runner, pipeline, request, history_days=1) == early
    event = early.events[0]
    assert isinstance(event, StructureFailedBreakoutEvent)
    with pytest.raises(FrozenInstanceError):
        event.range_high = 500
    with pytest.raises(StudyEventConflictError):
        store.save_study_result(replace(early, events=(replace(event, reclaim_close=99),)))
    varied = tuple(replace(bar, volume=float(index)) for index, bar in enumerate(raw[:4]))
    independent, *_ = run(tmp_path / "independent.db", varied)
    assert independent.events == early.events


def test_candidate_exclusivity_and_later_reentry(tmp_path: Path) -> None:
    raw = bars((100, 100, 112, 120, 100, 125, 100))
    result, *_ = run(tmp_path / "active.db", raw)
    assert [event.breakout_time_utc for event in result.events] == [
        raw[2].timestamp_utc,
        raw[5].timestamp_utc,
    ]
    assert [event.reclaim_time_utc for event in result.events] == [
        raw[4].timestamp_utc,
        raw[6].timestamp_utc,
    ]


def test_event_observation_failure_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "atomic.db"
    result, store, *_ = run(path, bars((100, 100, 112, 100)))
    with sqlite3.connect(path) as connection:
        tables = (
            "study_results",
            "structure_failed_breakout_events",
            "structure_failed_breakout_observations",
        )
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables
        }
        connection.execute(
            """CREATE TRIGGER reject_failed_breakout BEFORE INSERT
            ON structure_failed_breakout_observations
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        store.save_study_result(result)
    with sqlite3.connect(path) as connection:
        assert all(
            connection.execute(f"SELECT * FROM {table}").fetchall() == rows
            for table, rows in before.items()
        )


def test_no_lookahead_and_completion_cutoff() -> None:
    universe = tuple(
        replace(bar, instrument=INSTRUMENT, source=SOURCE)
        for bar in bars((100, 100, 112, 100, 120, 100, 100, 100, 100))
    )
    before = run_at(
        RangeFailedBreakoutStudy(),
        universe,
        "M5",
        end_of(3) - timedelta(microseconds=1),
        config=CONFIG,
    )
    assert not before.events
    assert (
        len(run_at(RangeFailedBreakoutStudy(), universe, "M5", end_of(3), config=CONFIG).events)
        == 1
    )
    report = assert_no_lookahead(
        blind(RangeFailedBreakoutStudy),
        universe,
        "M5",
        [end_of(3), end_of(5), end_of(7)],
        config=CONFIG,
    )
    assert report.cutoffs_checked == 3


@pytest.mark.parametrize(
    ("opens", "cutoff"),
    [
        (
            (
                datetime(2026, 3, 26, tzinfo=timezone.utc),
                datetime(2026, 3, 27, tzinfo=timezone.utc),
                datetime(2026, 3, 28, tzinfo=timezone.utc),
                datetime(2026, 3, 29, tzinfo=timezone.utc),
            ),
            datetime(2026, 3, 29, 23, tzinfo=timezone.utc),
        ),
        (
            (
                datetime(2026, 10, 21, 23, tzinfo=timezone.utc),
                datetime(2026, 10, 22, 23, tzinfo=timezone.utc),
                datetime(2026, 10, 23, 23, tzinfo=timezone.utc),
                datetime(2026, 10, 24, 23, tzinfo=timezone.utc),
            ),
            datetime(2026, 10, 26, tzinfo=timezone.utc),
        ),
    ],
)
def test_d1_calendar_dst(tmp_path: Path, opens: tuple[datetime, ...], cutoff: datetime) -> None:
    raw = tuple(
        replace(bar, timeframe="D1", timestamp_utc=timestamp)
        for bar, timestamp in zip(bars((100, 100, 112, 100)), opens)
    )
    calendar = SessionCalendar(
        calendar_id="synthetic",
        version="1",
        provider_id="test",
        instrument_id="GOLD",
        provenance=CalendarProvenance(source="synthetic", reference="failed-breakout"),
        timezone_name="Europe/London",
        effective_start_utc=opens[0] - timedelta(days=2),
        effective_end_utc=cutoff + timedelta(days=2),
        coverage_start_utc=opens[0] - timedelta(days=2),
        coverage_end_utc=cutoff + timedelta(days=2),
        weekly_sessions=tuple(
            WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(7)
        ),
    )
    valid = ValidatedBarSet.from_bars(raw, "D1", cutoff, cutoff_utc=cutoff, calendar=calendar)
    store = SQLitePersistence(tmp_path / "dst.db")
    store.initialize()
    registry = StudyRegistry()
    registry.register(RangeFailedBreakoutStudy())
    request = StudyRunRequest(
        "structure.range_failed_breakout", "0.1.0", "GOLD", "D1", cutoff, CONFIG
    )
    runner = StudyRunner(registry, store)
    result = runner.run(request, valid)
    assert len(result.events) == 1
    assert result.events[0].known_at_utc == cutoff
    assert runner.run(request, valid) == result


def test_schema_v11_rollback_preservation_and_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "migration.db"
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 10)
    store = SQLitePersistence(path)
    store.initialize()
    old, _, old_runner, old_request, old_valid = run_sweep(path, sweep_bars())
    assert old.events
    tables = (
        "study_results",
        "study_events",
        "study_event_observations",
        "structure_break_events",
        "structure_break_observations",
        "structure_zones",
        "structure_zone_observations",
        "structure_sweep_events",
        "structure_sweep_observations",
    )
    with sqlite3.connect(path) as connection:
        before = connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        rows = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 11)
    original = SQLitePersistence._migrate_to_version_11
    with monkeypatch.context() as patch:

        def fail(store: SQLitePersistence, connection: sqlite3.Connection) -> None:
            original(store, connection)
            raise sqlite3.OperationalError("injected")

        patch.setattr(SQLitePersistence, "_migrate_to_version_11", fail)
        with pytest.raises(MigrationError, match="rolled back"):
            store.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (10,)
        assert (
            connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
            == before
        )
        assert all(
            connection.execute(f"SELECT * FROM {table}").fetchall() == old_rows
            for table, old_rows in rows.items()
        )
    result, *_ = run(path, bars((100, 100, 112, 100)))
    assert result.events
    assert old_runner.run(old_request, old_valid) == old
    with sqlite3.connect(path) as connection:
        assert all(
            connection.execute(f"SELECT * FROM {table}").fetchall() == old_rows
            for table, old_rows in rows.items()
            if table != "study_results"
        )
        for table in ("structure_failed_breakout_events", "structure_failed_breakout_observations"):
            row = connection.execute(f"SELECT rowid, * FROM {table}").fetchone()
            columns = ", ".join(
                item[1] for item in connection.execute(f"PRAGMA table_info({table})")
            )
            placeholders = ", ".join("?" for _ in row[1:])
            for operation in (f"UPDATE {table} SET rowid=rowid", f"DELETE FROM {table}"):
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(operation)
            for recursive in (0, 1):
                connection.execute(f"PRAGMA recursive_triggers = {recursive}")
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(
                        f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})",
                        row[1:],
                    )
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(
                        f"INSERT OR REPLACE INTO {table} (rowid) VALUES (?)", row[:1]
                    )
                changed = list(row[1:])
                changed[0 if table.endswith("_events") else 1] = "different-id"
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(
                        f"INSERT OR REPLACE INTO {table} (rowid, {columns}) "
                        f"VALUES (?, {placeholders})",
                        (row[0], *changed),
                    )
