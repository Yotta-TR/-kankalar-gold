"""Synthetic point-in-time tests for descriptive zone sweeps."""

import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
from kankalar_gold_ai.core.contracts import (
    MarketBar,
    StructureSweepEvent,
    StructureSweepSide,
    StructureZone,
    StructureZoneType,
    StudyEventConflictError,
    StudyStatus,
    structure_zone_id,
)
from kankalar_gold_ai.data.calendar import CalendarProvenance, SessionCalendar, WeeklySession
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import MigrationError, SQLitePersistence
from kankalar_gold_ai.studies import (
    BosChochStudy,
    ConfirmedSwingsStudy,
    LiquiditySweepStudy,
    StudyConfigError,
    StudyRegistry,
    StudyRunner,
    StudyRunRequest,
    SupportResistanceZonesStudy,
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

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
CONFIG = {
    "left_bars": 1,
    "right_bars": 1,
    "lookback_bars": 5,
    "tolerance_bps": 100,
    "min_sweep_bps": 100,
}


def bars(count: int = 6) -> tuple[MarketBar, ...]:
    result = [
        MarketBar(
            "GOLD",
            "M5",
            START + timedelta(minutes=5 * i),
            100,
            110 if i % 2 else 105,
            90 if i % 2 else 95,
            100,
            None,
            "test",
        )
        for i in range(count)
    ]
    if count > 5:
        result[5] = replace(result[5], high=111.1, low=89.1)
    return tuple(result)


def run(path: Path, raw: tuple[MarketBar, ...], config: dict[str, int] = CONFIG):
    cutoff = raw[-1].timestamp_utc + timedelta(minutes=5)
    valid = ValidatedBarSet.from_bars(raw, "M5", cutoff, cutoff_utc=cutoff)
    store = SQLitePersistence(path)
    store.initialize()
    registry = StudyRegistry()
    registry.register(LiquiditySweepStudy())
    runner = StudyRunner(registry, store)
    request = StudyRunRequest("structure.liquidity_sweep", "0.1.0", "GOLD", "M5", cutoff, config)
    return runner.run(request, valid), store, runner, request, valid


def zone(
    zone_type: StructureZoneType,
    level: float,
    known_minute: int,
    calendar_id: str,
    suffix: str = "a",
) -> StructureZone:
    ids = (f"{suffix}-1", f"{suffix}-2")
    prices = (float(level), float(level))
    known = START + timedelta(minutes=known_minute)
    identity = structure_zone_id(
        "structure.sr_zones",
        "0.1.0",
        "GOLD",
        "M5",
        zone_type,
        ids,
        prices,
        known,
        5,
        100.0,
        calendar_id,
    )
    return StructureZone(
        identity,
        "structure.sr_zones",
        "0.1.0",
        "GOLD",
        "M5",
        zone_type,
        ids,
        prices,
        level,
        level,
        known,
        5,
        100.0,
        calendar_id,
    )


def inject(monkeypatch: pytest.MonkeyPatch, definitions: tuple[tuple, ...]) -> None:
    def build(self, bars, swings, config, cutoff, calendar_id):
        return tuple(zone(*definition, calendar_id) for definition in definitions)

    monkeypatch.setattr(SupportResistanceZonesStudy, "_zones_from_swings", build)


@pytest.mark.parametrize(
    ("side", "previous", "wick", "close", "expected"),
    [
        ("high", 100, 101, 100, True),
        ("high", 100, 100.999999, 100, False),
        ("high", 100, 101.000001, 100, True),
        ("high", 100, 100, 100, False),
        ("high", 100.01, 102, 100, False),
        ("high", 99, 102, 100.01, False),
        ("low", 100, 99, 100, True),
        ("low", 100, 99.000001, 100, False),
        ("low", 100, 98.999999, 100, True),
        ("low", 100, 100, 100, False),
        ("low", 99.99, 98, 100, False),
        ("low", 101, 98, 99.99, False),
    ],
)
def test_sweep_boundaries_and_breakout_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    previous: float,
    wick: float,
    close: float,
    expected: bool,
) -> None:
    kind = StructureZoneType.RESISTANCE if side == "high" else StructureZoneType.SUPPORT
    inject(monkeypatch, ((kind, 100.0, 25),))
    raw = list(bars())
    raw[4] = replace(raw[4], close=previous, high=max(105, previous), low=min(95, previous))
    high = max(100.0, wick, close) if side == "high" else max(100.0, close)
    low = min(100.0, close) if side == "high" else min(100.0, wick, close)
    raw[5] = replace(raw[5], high=high, low=low, close=close)
    result, *_ = run(tmp_path / "boundary.db", tuple(raw))
    assert bool(result.events) is expected
    if expected:
        event = result.events[0]
        assert isinstance(event, StructureSweepEvent)
        assert event.side is (
            StructureSweepSide.HIGH_SIDE if side == "high" else StructureSweepSide.LOW_SIDE
        )
        assert event.previous_close == previous
        assert event.wick_price == wick
        assert event.close == close


def test_zone_known_at_open_and_two_sides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inject(
        monkeypatch,
        (
            (StructureZoneType.RESISTANCE, 100.0, 25),
            (StructureZoneType.SUPPORT, 100.0, 25),
        ),
    )
    raw = list(bars())
    raw[5] = replace(raw[5], high=101, low=99)
    result, *_ = run(tmp_path / "both.db", tuple(raw))
    assert {event.side for event in result.events} == set(StructureSweepSide)
    assert all(event.sweep_bar_time_utc == raw[5].timestamp_utc for event in result.events)
    inject(
        monkeypatch,
        (
            (StructureZoneType.RESISTANCE, 100.0, 26),
            (StructureZoneType.SUPPORT, 100.0, 26),
        ),
    )
    late, *_ = run(tmp_path / "late.db", tuple(raw))
    assert not late.events


def test_nearest_zone_and_tie_breaks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def build(self, bars, swings, config, cutoff, calendar_id):
        return (
            zone(StructureZoneType.RESISTANCE, 101, 20, calendar_id, "far"),
            zone(StructureZoneType.RESISTANCE, 100, 15, calendar_id, "early"),
            zone(StructureZoneType.RESISTANCE, 100, 25, calendar_id, "late-a"),
            zone(StructureZoneType.RESISTANCE, 100, 25, calendar_id, "late-b"),
            zone(StructureZoneType.SUPPORT, 99, 20, calendar_id, "low-far"),
            zone(StructureZoneType.SUPPORT, 100, 25, calendar_id, "low-near"),
        )

    monkeypatch.setattr(SupportResistanceZonesStudy, "_zones_from_swings", build)
    raw = list(bars())
    raw[5] = replace(raw[5], high=103, low=97)
    result, *_ = run(tmp_path / "tie.db", tuple(raw))
    high, low = result.events
    assert high.side is StructureSweepSide.HIGH_SIDE and high.reference_zone.upper_price == 100
    assert high.reference_zone.known_at_utc == START + timedelta(minutes=25)
    expected_ids = [
        zone(
            StructureZoneType.RESISTANCE, 100, 25, high.reference_zone.calendar_context_id, suffix
        ).event_id
        for suffix in ("late-a", "late-b")
    ]
    assert high.reference_zone.event_id == max(expected_ids)
    assert low.side is StructureSweepSide.LOW_SIDE and low.reference_zone.lower_price == 100


@pytest.mark.parametrize("key", list(CONFIG))
def test_config_has_no_defaults(key: str) -> None:
    with pytest.raises(StudyConfigError):
        LiquiditySweepStudy().validate_config({k: v for k, v in CONFIG.items() if k != key})


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan"), "1", 10**400])
def test_threshold_must_be_positive_finite_number(value: object) -> None:
    with pytest.raises(StudyConfigError):
        LiquiditySweepStudy().validate_config({**CONFIG, "min_sweep_bps": value})


def test_real_zone_membership_replay_pipeline_and_cache(tmp_path: Path) -> None:
    raw = bars(8)
    early, store, runner, request, valid = run(tmp_path / "replay.db", raw[:6])
    assert len(early.events) == 2
    assert all(event.reference_zone.known_at_utc == raw[5].timestamp_utc for event in early.events)
    assert runner.run(request, valid) == early
    with sqlite3.connect(tmp_path / "replay.db") as connection:
        before_zones = connection.execute(
            "SELECT * FROM structure_zones ORDER BY event_id"
        ).fetchall()
        assert len(before_zones) == 2
    later, *_ = run(tmp_path / "replay.db", raw)
    assert set(early.events).issubset(later.events)
    assert runner.run(request, valid) == early
    with sqlite3.connect(tmp_path / "replay.db") as connection:
        assert all(
            row in connection.execute("SELECT * FROM structure_zones").fetchall()
            for row in before_zones
        )
    varied = tuple(replace(bar, volume=float(i + 1)) for i, bar in enumerate(raw[:6]))
    other, *_ = run(tmp_path / "volume.db", varied)
    assert other.events == early.events
    registry = StudyRegistry()
    registry.register(LiquiditySweepStudy())
    pipeline_store = SQLitePersistence(tmp_path / "pipeline.db")
    pipeline_store.initialize()
    pipeline = HistoricalDataPipeline(MockMarketDataProvider(raw), clock=lambda: valid.cutoff_utc)
    assert (
        run_pipeline_study(StudyRunner(registry, pipeline_store), pipeline, request, history_days=1)
        == early
    )
    assert (
        StudyRunner(registry, SQLitePersistence(tmp_path / "replay.db")).run(request, valid)
        == early
    )


def test_no_lookahead_and_cutoff(tmp_path: Path) -> None:
    raw = bars(12)
    universe = tuple(replace(bar, instrument=INSTRUMENT, source=SOURCE) for bar in raw)
    just_before = run_at(
        LiquiditySweepStudy(), universe, "M5", end_of(5) - timedelta(microseconds=1), config=CONFIG
    )
    assert just_before.status is StudyStatus.INSUFFICIENT_DATA
    assert not just_before.events
    assert run_at(LiquiditySweepStudy(), universe, "M5", end_of(5), config=CONFIG).events
    report = assert_no_lookahead(
        blind(LiquiditySweepStudy), universe, "M5", [end_of(5), end_of(7), end_of(9)], config=CONFIG
    )
    assert report.cutoffs_checked == 3
    early, *_ = run(tmp_path / "early.db", raw[:5])
    assert early.status is StudyStatus.INSUFFICIENT_DATA


def test_repeated_sweeps_use_the_zone_at_each_candle_open(tmp_path: Path) -> None:
    raw = list(bars(8))
    raw[7] = replace(raw[7], high=113, low=95)
    result, *_ = run(tmp_path / "repeated.db", tuple(raw))
    high_events = [event for event in result.events if event.side is StructureSweepSide.HIGH_SIDE]
    assert [event.sweep_bar_time_utc for event in high_events] == [
        raw[5].timestamp_utc,
        raw[7].timestamp_utc,
    ]
    assert high_events[0].event_id != high_events[1].event_id
    assert high_events[0].reference_zone.event_id != high_events[1].reference_zone.event_id
    zone_config = {key: value for key, value in CONFIG.items() if key != "min_sweep_bps"}
    registry = StudyRegistry()
    registry.register(SupportResistanceZonesStudy())
    store = SQLitePersistence(tmp_path / "zones.db")
    store.initialize()
    runner = StudyRunner(registry, store)
    for event, index in zip(high_events, (5, 7)):
        prefix = tuple(raw[:index])
        cutoff = raw[index].timestamp_utc
        valid = ValidatedBarSet.from_bars(prefix, "M5", cutoff, cutoff_utc=cutoff)
        request = StudyRunRequest("structure.sr_zones", "0.1.0", "GOLD", "M5", cutoff, zone_config)
        zone_result = runner.run(request, valid)
        assert event.reference_zone in zone_result.events


def test_atomic_rollback_on_sweep_observation_failure(tmp_path: Path) -> None:
    path = tmp_path / "atomic.db"
    result, store, *_ = run(path, bars())
    with sqlite3.connect(path) as connection:
        tables = (
            "study_results",
            "structure_zones",
            "structure_sweep_events",
            "structure_sweep_observations",
        )
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            for table in tables
        }
        connection.execute(
            """CREATE TRIGGER reject_sweep_observation BEFORE INSERT ON structure_sweep_observations
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        store.save_study_result(result)
    with sqlite3.connect(path) as connection:
        assert all(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == count
            for table, count in before.items()
        )


@pytest.mark.parametrize(
    ("opens", "cutoff"),
    [
        (
            (
                datetime(2026, 3, 26, tzinfo=timezone.utc),
                datetime(2026, 3, 27, tzinfo=timezone.utc),
                datetime(2026, 3, 28, tzinfo=timezone.utc),
                datetime(2026, 3, 29, tzinfo=timezone.utc),
                datetime(2026, 3, 29, 23, tzinfo=timezone.utc),
                datetime(2026, 3, 30, 23, tzinfo=timezone.utc),
            ),
            datetime(2026, 3, 31, 23, tzinfo=timezone.utc),
        ),
        (
            (
                datetime(2026, 10, 22, 23, tzinfo=timezone.utc),
                datetime(2026, 10, 23, 23, tzinfo=timezone.utc),
                datetime(2026, 10, 24, 23, tzinfo=timezone.utc),
                datetime(2026, 10, 26, tzinfo=timezone.utc),
                datetime(2026, 10, 27, tzinfo=timezone.utc),
                datetime(2026, 10, 28, tzinfo=timezone.utc),
            ),
            datetime(2026, 10, 29, tzinfo=timezone.utc),
        ),
    ],
)
def test_d1_calendar_dst(tmp_path: Path, opens: tuple[datetime, ...], cutoff: datetime) -> None:
    template = bars()
    raw = tuple(
        replace(bar, timeframe="D1", timestamp_utc=timestamp)
        for bar, timestamp in zip(template, opens)
    )
    calendar = SessionCalendar(
        calendar_id="synthetic",
        version="1",
        provider_id="test",
        instrument_id="GOLD",
        provenance=CalendarProvenance(source="synthetic", reference="sweeps"),
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
    registry.register(LiquiditySweepStudy())
    request = StudyRunRequest("structure.liquidity_sweep", "0.1.0", "GOLD", "D1", cutoff, CONFIG)
    runner = StudyRunner(registry, store)
    result = runner.run(request, valid)
    assert len(result.events) == 2
    assert all(
        event.known_at_utc == cutoff and event.reference_zone.known_at_utc == opens[5]
        for event in result.events
    )
    assert runner.run(request, valid) == result


def test_immutable_event_conflict_and_sql_guards(tmp_path: Path) -> None:
    path = tmp_path / "guards.db"
    result, store, *_ = run(path, bars())
    event = result.events[0]
    assert isinstance(event, StructureSweepEvent)
    with pytest.raises(FrozenInstanceError):
        event.close = 99
    with sqlite3.connect(path) as connection:
        before = connection.execute("SELECT COUNT(*) FROM study_results").fetchone()
    with pytest.raises(StudyEventConflictError):
        store.save_study_result(replace(result, events=(replace(event, close=99),)))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_results").fetchone() == before
        for table in ("structure_sweep_events", "structure_sweep_observations"):
            record = connection.execute(f"SELECT rowid, * FROM {table} LIMIT 1").fetchone()
            rowid, *values = record
            columns = ", ".join(
                item[1] for item in connection.execute(f"PRAGMA table_info({table})")
            )
            placeholders = ", ".join("?" for _ in values)
            for operation in (f"UPDATE {table} SET rowid=rowid", f"DELETE FROM {table}"):
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(operation)
            for recursive in (0, 1):
                connection.execute(f"PRAGMA recursive_triggers = {recursive}")
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(
                        f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})",
                        values,
                    )
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(
                        f"INSERT OR REPLACE INTO {table} (rowid) VALUES (?)", (rowid,)
                    )
                changed = list(values)
                changed[0 if table == "structure_sweep_events" else 1] = "different-id"
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(
                        f"INSERT OR REPLACE INTO {table} (rowid, {columns}) "
                        f"VALUES (?, {placeholders})",
                        (rowid, *changed),
                    )
            assert connection.execute(f"SELECT rowid, * FROM {table} LIMIT 1").fetchone() == record


def test_v9_migration_rollback_and_preservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "migration.db"
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 9)
    store = SQLitePersistence(path)
    store.initialize()
    raw = bars(5)
    cutoff = START + timedelta(minutes=25)
    valid = ValidatedBarSet.from_bars(raw, "M5", cutoff, cutoff_utc=cutoff)
    registry = StudyRegistry()
    registry.register(SupportResistanceZonesStudy())
    zone_config = {key: value for key, value in CONFIG.items() if key != "min_sweep_bps"}
    request = StudyRunRequest("structure.sr_zones", "0.1.0", "GOLD", "M5", cutoff, zone_config)
    old = StudyRunner(registry, store).run(request, valid)
    assert old.events
    registry.register(ConfirmedSwingsStudy())
    swing_request = StudyRunRequest(
        "structure.swings", "0.1.0", "GOLD", "M5", cutoff, {"left_bars": 1, "right_bars": 1}
    )
    old_swings = StudyRunner(registry, store).run(swing_request, valid)
    assert old_swings.events
    registry.register(BosChochStudy())
    rows = ((6, 7, 5, 6), (7, 10, 6, 8), (8, 9, 6, 7), (7, 12, 6, 11))
    break_bars = tuple(
        MarketBar("GOLD", "M5", START + timedelta(minutes=5 * i), *row, None, "test")
        for i, row in enumerate(rows)
    )
    break_cutoff = START + timedelta(minutes=20)
    break_valid = ValidatedBarSet.from_bars(break_bars, "M5", break_cutoff, cutoff_utc=break_cutoff)
    break_request = StudyRunRequest(
        "structure.bos_choch",
        "0.1.0",
        "GOLD",
        "M5",
        break_cutoff,
        {"left_bars": 1, "right_bars": 1},
    )
    old_breaks = StudyRunner(registry, store).run(break_request, break_valid)
    assert old_breaks.events
    tables = (
        "study_results",
        "study_events",
        "study_event_observations",
        "structure_break_events",
        "structure_break_observations",
        "structure_zones",
        "structure_zone_observations",
    )
    with sqlite3.connect(path) as connection:
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables
        }
        schema = connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 10)
    original = SQLitePersistence._migrate_to_version_10
    with monkeypatch.context() as patch:

        def fail(store: SQLitePersistence, connection: sqlite3.Connection) -> None:
            original(store, connection)
            raise sqlite3.OperationalError("injected failure")

        patch.setattr(SQLitePersistence, "_migrate_to_version_10", fail)
        with pytest.raises(MigrationError, match="rolled back"):
            store.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (9,)
        assert (
            connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
            == schema
        )
        assert all(
            connection.execute(f"SELECT * FROM {table}").fetchall() == rows
            for table, rows in before.items()
        )
    store.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (10,)
        assert all(
            connection.execute(f"SELECT * FROM {table}").fetchall() == rows
            for table, rows in before.items()
        )
    assert StudyRunner(registry, store).run(request, valid) == old
    assert StudyRunner(registry, store).run(swing_request, valid) == old_swings
    assert StudyRunner(registry, store).run(break_request, break_valid) == old_breaks
