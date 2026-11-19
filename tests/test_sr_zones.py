"""Synthetic, point-in-time support/resistance zone regressions."""

import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
from kankalar_gold_ai.core.contracts import (
    MarketBar,
    StructureZone,
    StructureZoneType,
    StudyEvent,
    StudyEventType,
    StudyStatus,
    study_event_id,
)
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import (
    MigrationError,
    SQLitePersistence,
    StoredStudyResultError,
)
from kankalar_gold_ai.studies import (
    BosChochStudy,
    ConfirmedSwingsStudy,
    StudyConfigError,
    StudyOutput,
    StudyPersistenceError,
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
CONFIG = {"left_bars": 1, "right_bars": 1, "lookback_bars": 5, "tolerance_bps": 100}


def bars(count: int = 7) -> tuple[MarketBar, ...]:
    return tuple(
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
    )


def run(path: Path, raw: tuple[MarketBar, ...], config: dict[str, int] = CONFIG):
    cutoff = raw[-1].timestamp_utc + timedelta(minutes=5)
    valid = ValidatedBarSet.from_bars(raw, "M5", cutoff, cutoff_utc=cutoff)
    store = SQLitePersistence(path)
    store.initialize()
    registry = StudyRegistry()
    registry.register(SupportResistanceZonesStudy())
    runner = StudyRunner(registry, store)
    request = StudyRunRequest("structure.sr_zones", "0.1.0", "GOLD", "M5", cutoff, config)
    return runner.run(request, valid), store, runner, request, valid


def swing(event_type: StudyEventType, price: float, index: int, known_index: int) -> StudyEvent:
    pivot = START + timedelta(minutes=5 * index)
    return StudyEvent(
        study_event_id("structure.swings", "0.1.0", "GOLD", "M5", event_type, pivot, 1, 1),
        "structure.swings",
        "0.1.0",
        "GOLD",
        "M5",
        event_type,
        pivot,
        START + timedelta(minutes=5 * known_index),
        price,
        1,
        1,
    )


def inject(monkeypatch: pytest.MonkeyPatch, events: tuple[StudyEvent, ...]) -> None:
    def compute(self: ConfirmedSwingsStudy, bars: object, config: object, context: object):
        return StudyOutput(status=StudyStatus.AVAILABLE, events=events)

    monkeypatch.setattr(ConfirmedSwingsStudy, "compute_with_context", compute)


@pytest.mark.parametrize(
    ("event_type", "zone_type"),
    [
        (StudyEventType.SWING_LOW, StructureZoneType.SUPPORT),
        (StudyEventType.SWING_HIGH, StructureZoneType.RESISTANCE),
    ],
)
def test_complete_linkage_exact_boundary_and_multiple_clusters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_type: StudyEventType,
    zone_type: StructureZoneType,
) -> None:
    events = tuple(swing(event_type, p, i, 6) for i, p in enumerate((100, 101, 102, 103.02)))
    inject(monkeypatch, tuple(reversed(events)))
    result, *_ = run(tmp_path / "clusters.db", bars(), {**CONFIG, "lookback_bars": 7})
    zones = sorted(result.events, key=lambda zone: zone.lower_price)
    assert len(zones) == 2
    assert all(isinstance(zone, StructureZone) and zone.zone_type is zone_type for zone in zones)
    assert [(zone.lower_price, zone.upper_price) for zone in zones] == [(100, 101), (102, 103.02)]
    assert [zone.member_ids for zone in zones] == [
        tuple(event.event_id for event in events[:2]),
        tuple(event.event_id for event in events[2:]),
    ]
    assert all(zone.known_at_utc == START + timedelta(minutes=30) for zone in zones)


def test_outside_tolerance_and_transitive_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    events = tuple(
        swing(StudyEventType.SWING_LOW, p, i, 6) for i, p in enumerate((100, 100.9, 101.000001))
    )
    inject(monkeypatch, events)
    result, *_ = run(tmp_path / "chain.db", bars(), {**CONFIG, "lookback_bars": 7})
    assert len(result.events) == 1
    assert result.events[0].member_ids == tuple(event.event_id for event in events[:2])
    assert result.events[0].upper_price == 100.9


def test_single_swing_and_cutoff_and_pivot_lookback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = (
        swing(StudyEventType.SWING_LOW, 100, 1, 4),
        swing(StudyEventType.SWING_LOW, 100, 2, 8),
        swing(StudyEventType.SWING_LOW, 100, 3, 5),
    )
    inject(monkeypatch, events)
    result, *_ = run(tmp_path / "cutoff.db", bars())
    assert result.events == ()


@pytest.mark.parametrize(
    "key,value",
    [
        ("lookback_bars", 0),
        ("lookback_bars", True),
        ("tolerance_bps", 0),
        ("tolerance_bps", float("inf")),
        ("tolerance_bps", True),
    ],
)
def test_configuration_is_explicit_and_positive(key: str, value: object) -> None:
    study = SupportResistanceZonesStudy()
    with pytest.raises(StudyConfigError):
        study.validate_config({**CONFIG, key: value})
    with pytest.raises(StudyConfigError):
        study.validate_config({k: v for k, v in CONFIG.items() if k != key})


def test_real_swings_confirmation_same_bar_cache_and_replay(tmp_path: Path) -> None:
    raw = bars()
    early, store, runner, request, valid = run(tmp_path / "zones.db", raw[:5])
    assert len(early.events) == 2
    assert {zone.zone_type for zone in early.events} == set(StructureZoneType)
    assert all(zone.known_at_utc == START + timedelta(minutes=25) for zone in early.events)
    assert runner.run(request, valid) == early
    reopened = SQLitePersistence(tmp_path / "zones.db")
    assert StudyRunner(runner._registry, reopened).run(request, valid) == early
    later, *_ = run(tmp_path / "zones.db", raw)
    assert len(later.events) == 2
    assert {z.event_id for z in early.events}.isdisjoint(z.event_id for z in later.events)
    assert all(set(a.member_ids) & set(b.member_ids) for a, b in zip(early.events, later.events))
    with sqlite3.connect(tmp_path / "zones.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM structure_zones").fetchone() == (4,)
    assert runner.run(request, valid) == early
    varied = tuple(replace(bar, volume=float(i + 1)) for i, bar in enumerate(raw[:5]))
    other, *_ = run(tmp_path / "volume.db", varied)
    assert other.events == early.events


def test_real_confirmation_boundary_and_warmup(tmp_path: Path) -> None:
    config = {**CONFIG, "lookback_bars": 4}
    before, *_ = run(tmp_path / "before.db", bars(4), config)
    assert before.status is StudyStatus.AVAILABLE
    assert before.events == ()
    at_confirmation, *_ = run(tmp_path / "confirmed.db", bars(5), config)
    assert len(at_confirmation.events) == 2
    assert all(
        zone.known_at_utc == START + timedelta(minutes=25) for zone in at_confirmation.events
    )
    short, *_ = run(tmp_path / "short.db", bars(3), config)
    assert short.status is StudyStatus.INSUFFICIENT_DATA


def test_zone_contract_and_corrupt_cache_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    result, store, runner, request, valid = run(path, bars(5))
    zone = result.events[0]
    assert isinstance(zone, StructureZone)
    with pytest.raises(FrozenInstanceError):
        zone.lower_price = 1
    with pytest.raises(ValueError, match="bounds"):
        replace(zone, lower_price=zone.lower_price - 1)
    with pytest.raises(ValueError, match="identity"):
        replace(zone, event_id="wrong-id")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER structure_zones_no_update")
        connection.execute("UPDATE structure_zones SET member_prices_json = '[]'")
    with pytest.raises(StudyPersistenceError) as caught:
        runner.run(request, valid)
    assert isinstance(caught.value.__cause__, StoredStudyResultError)


def test_zone_observation_failure_rolls_back_result_and_records(tmp_path: Path) -> None:
    path = tmp_path / "atomic.db"
    result, store, *_ = run(path, bars(5))
    with sqlite3.connect(path) as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            for table in ("study_results", "structure_zones", "structure_zone_observations")
        }
        connection.execute(
            """CREATE TRIGGER reject_zone_observation BEFORE INSERT ON structure_zone_observations
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        store.save_study_result(result)
    with sqlite3.connect(path) as connection:
        assert all(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == count
            for table, count in before.items()
        )


def test_pipeline_and_no_lookahead(tmp_path: Path) -> None:
    raw = bars(12)
    initial, _store, _runner, request, valid = run(tmp_path / "direct.db", raw[:5])
    registry = StudyRegistry()
    registry.register(SupportResistanceZonesStudy())
    store = SQLitePersistence(tmp_path / "pipeline.db")
    store.initialize()
    pipeline = HistoricalDataPipeline(MockMarketDataProvider(raw), clock=lambda: valid.cutoff_utc)
    assert (
        run_pipeline_study(StudyRunner(registry, store), pipeline, request, history_days=1)
        == initial
    )
    universe = tuple(replace(bar, instrument=INSTRUMENT, source=SOURCE) for bar in raw)
    assert run_at(SupportResistanceZonesStudy(), universe, "M5", end_of(4), config=CONFIG).events
    assert_no_lookahead(
        blind(SupportResistanceZonesStudy),
        universe,
        "M5",
        [end_of(4), end_of(6), end_of(8)],
        config=CONFIG,
    )


def test_append_only_and_replace_guards(tmp_path: Path) -> None:
    path = tmp_path / "guards.db"
    result, *_ = run(path, bars(5))
    assert result.events
    with sqlite3.connect(path) as connection:
        for table in ("structure_zones", "structure_zone_observations"):
            record = connection.execute(f"SELECT rowid, * FROM {table} LIMIT 1").fetchone()
            rowid, *values = record
            columns = ", ".join(
                item[1] for item in connection.execute(f"PRAGMA table_info({table})")
            )
            placeholders = ", ".join("?" for _ in values)
            for operation in (f"UPDATE {table} SET rowid = rowid", f"DELETE FROM {table}"):
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(operation)
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(
                    f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})", values
                )
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(f"INSERT OR REPLACE INTO {table} (rowid) VALUES (?)", (rowid,))
            values[0 if table == "structure_zones" else 1] = "different-id"
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(
                    f"INSERT OR REPLACE INTO {table} (rowid, {columns}) VALUES (?, {placeholders})",
                    (rowid, *values),
                )
            assert connection.execute(f"SELECT rowid, * FROM {table} LIMIT 1").fetchone() == record


def test_v8_migration_preserves_rows_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "migration.db"
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 8)
    store = SQLitePersistence(path)
    store.initialize()
    raw = bars(5)
    cutoff = START + timedelta(minutes=25)
    valid = ValidatedBarSet.from_bars(raw, "M5", cutoff, cutoff_utc=cutoff)
    registry = StudyRegistry()
    registry.register(ConfirmedSwingsStudy())
    request = StudyRunRequest(
        "structure.swings", "0.1.0", "GOLD", "M5", cutoff, {"left_bars": 1, "right_bars": 1}
    )
    old = StudyRunner(registry, store).run(request, valid)
    registry.register(BosChochStudy())
    break_rows = (
        (6, 7, 5, 6),
        (7, 10, 6, 8),
        (8, 9, 6, 7),
        (7, 12, 6, 11),
    )
    breaks = tuple(
        MarketBar("GOLD", "M5", START + timedelta(minutes=5 * i), *row, None, "test")
        for i, row in enumerate(break_rows)
    )
    break_cutoff = START + timedelta(minutes=20)
    break_valid = ValidatedBarSet.from_bars(breaks, "M5", break_cutoff, cutoff_utc=break_cutoff)
    break_request = StudyRunRequest(
        "structure.bos_choch",
        "0.1.0",
        "GOLD",
        "M5",
        break_cutoff,
        {"left_bars": 1, "right_bars": 1},
    )
    old_break = StudyRunner(registry, store).run(break_request, break_valid)
    assert old_break.events
    with sqlite3.connect(path) as connection:
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "study_results",
                "study_events",
                "study_event_observations",
                "structure_break_events",
                "structure_break_observations",
            )
        }
        schema = connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 9)
    original = SQLitePersistence._migrate_to_version_9
    with monkeypatch.context() as patch:

        def fail(store: SQLitePersistence, connection: sqlite3.Connection) -> None:
            original(store, connection)
            raise sqlite3.OperationalError("injected failure")

        patch.setattr(SQLitePersistence, "_migrate_to_version_9", fail)
        with pytest.raises(MigrationError, match="rolled back"):
            store.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (8,)
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
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (9,)
        assert all(
            connection.execute(f"SELECT * FROM {table}").fetchall() == rows
            for table, rows in before.items()
        )
    assert StudyRunner(registry, store).run(request, valid) == old
    assert StudyRunner(registry, store).run(break_request, break_valid) == old_break
