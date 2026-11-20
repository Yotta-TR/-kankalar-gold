"""Point-in-time tests for immutable three-candle price-imbalance events."""

import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
from kankalar_gold_ai.core.contracts import (
    Direction,
    MarketBar,
    StructureFvgEvent,
)
from kankalar_gold_ai.data.calendar import CalendarProvenance, SessionCalendar, WeeklySession
from kankalar_gold_ai.data.errors import HistoricalDataQualityError
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import MigrationError, SQLitePersistence
from kankalar_gold_ai.studies import (
    StructureFvgStudy,
    StudyConfigError,
    StudyRegistry,
    StudyRunner,
    StudyRunRequest,
    run_pipeline_study,
)
from tests.test_liquidity_sweep import bars as sweep_bars
from tests.test_liquidity_sweep import run as run_sweep

START = datetime(2026, 1, 5, tzinfo=timezone.utc)
STUDY_ID = "structure.gaps_fvg"
VERSION = "0.1.0"
INSTRUMENT = "GOLD"
SOURCE = "synthetic"
CONFIG: dict[str, str | int | float | bool | None] = {}


def bar(
    index: int,
    low: float,
    high: float,
    *,
    timestamp: datetime | None = None,
    timeframe: str = "M5",
) -> MarketBar:
    opening = (low + high) / 2
    return MarketBar(
        INSTRUMENT,
        timeframe,
        START + timedelta(minutes=5 * index) if timestamp is None else timestamp,
        opening,
        high,
        low,
        opening,
        None,
        SOURCE,
    )


def bullish_bars() -> tuple[MarketBar, ...]:
    return (bar(0, 90, 100), bar(1, 95, 120), bar(2, 101, 110))


def bearish_bars() -> tuple[MarketBar, ...]:
    return (bar(0, 100, 110), bar(1, 80, 105), bar(2, 89, 99))


def calendar(
    *,
    zone: str = "UTC",
    sessions: tuple[WeeklySession, ...] | None = None,
    end: datetime = datetime(2027, 1, 1, tzinfo=timezone.utc),
) -> SessionCalendar:
    return SessionCalendar(
        calendar_id="synthetic-fvg",
        version="1",
        provider_id=SOURCE,
        instrument_id=INSTRUMENT,
        provenance=CalendarProvenance(source="synthetic", reference="FVG tests"),
        timezone_name=zone,
        effective_start_utc=START - timedelta(days=30),
        effective_end_utc=end,
        coverage_start_utc=START - timedelta(days=30),
        coverage_end_utc=end,
        weekly_sessions=sessions
        if sessions is not None
        else tuple(WeeklySession(day, time(0), time(0), 1) for day in range(7)),
    )


def run(
    path: Path,
    raw: tuple[MarketBar, ...],
    *,
    schedule: SessionCalendar | None = None,
    expected_open=None,
):
    cutoff = raw[-1].timestamp_utc + timedelta(minutes=5)
    valid = ValidatedBarSet.from_bars(
        raw,
        raw[0].timeframe,
        cutoff,
        cutoff_utc=cutoff,
        calendar=schedule,
        expected_open=expected_open,
    )
    store = SQLitePersistence(path)
    store.initialize()
    registry = StudyRegistry()
    registry.register(StructureFvgStudy())
    runner = StudyRunner(registry, store)
    request = StudyRunRequest(STUDY_ID, VERSION, INSTRUMENT, raw[0].timeframe, cutoff, CONFIG)
    return runner.run(request, valid), store, runner, request, valid


def test_bullish_bearish_equality_and_no_gap(tmp_path: Path) -> None:
    bullish, *_ = run(tmp_path / "bullish.db", bullish_bars())
    bearish, *_ = run(tmp_path / "bearish.db", bearish_bars())
    assert len(bullish.events) == len(bearish.events) == 1
    up = bullish.events[0]
    down = bearish.events[0]
    assert isinstance(up, StructureFvgEvent) and isinstance(down, StructureFvgEvent)
    assert (up.direction, up.lower_price, up.upper_price) == (Direction.BULLISH, 100, 101)
    assert (down.direction, down.lower_price, down.upper_price) == (Direction.BEARISH, 99, 100)
    assert (
        up.first_candle_time_utc,
        up.middle_candle_time_utc,
        up.third_candle_time_utc,
        up.known_at_utc,
    ) == (
        bullish_bars()[0].timestamp_utc,
        bullish_bars()[1].timestamp_utc,
        bullish_bars()[2].timestamp_utc,
        START + timedelta(minutes=15),
    )

    equal_up = (bar(0, 90, 100), bar(1, 95, 120), bar(2, 100, 110))
    equal_down = (bar(0, 100, 110), bar(1, 80, 105), bar(2, 90, 100))
    no_gap = (bar(0, 90, 100), bar(1, 95, 120), bar(2, 95, 105))
    for index, raw in enumerate((equal_up, equal_down, no_gap)):
        result, *_ = run(tmp_path / f"none-{index}.db", raw)
        assert result.events == ()


@pytest.mark.parametrize(
    ("width", "direction"), [(1e-12, Direction.BULLISH), (1e6, Direction.BULLISH)]
)
def test_positive_gap_width_has_no_threshold(
    width: float, direction: Direction, tmp_path: Path
) -> None:
    raw = (bar(0, 90, 100), bar(1, 90, 1_000_100), bar(2, 100 + width, 110 + width))
    result, *_ = run(tmp_path / f"width-{width}.db", raw)
    assert len(result.events) == 1
    assert result.events[0].direction is direction
    assert result.events[0].upper_price - result.events[0].lower_price == pytest.approx(width)


def test_only_completed_bars_at_the_cutoff_are_consumed(tmp_path: Path) -> None:
    raw = bullish_bars()
    third_completion = raw[-1].timestamp_utc + timedelta(minutes=5)
    with pytest.raises(ValueError, match="complete after cutoff"):
        ValidatedBarSet.from_bars(
            raw,
            "M5",
            third_completion,
            cutoff_utc=third_completion - timedelta(microseconds=1),
        )
    result, *_ = run(tmp_path / "at-cutoff.db", raw)
    assert result.events[0].known_at_utc == third_completion


def test_missing_and_nonconsecutive_bars_fail_closed(tmp_path: Path) -> None:
    raw = bullish_bars()
    shifted_middle = replace(raw[1], timestamp_utc=raw[1].timestamp_utc + timedelta(minutes=5))
    shifted_third = replace(raw[2], timestamp_utc=raw[2].timestamp_utc + timedelta(minutes=5))
    discontinuous = (raw[0], shifted_middle, shifted_third)
    result, *_ = run(
        tmp_path / "nonconsecutive.db",
        discontinuous,
        expected_open=lambda _timestamp: False,
    )
    assert result.events == ()

    open_sessions = tuple(WeeklySession(day, time(0), time(0), 1) for day in range(7))
    schedule = calendar(sessions=open_sessions)
    missing_open_bar = (raw[0], replace(raw[1], timestamp_utc=raw[2].timestamp_utc))
    with pytest.raises(HistoricalDataQualityError):
        ValidatedBarSet.from_bars(
            missing_open_bar,
            "M5",
            raw[-1].timestamp_utc + timedelta(minutes=5),
            cutoff_utc=raw[-1].timestamp_utc + timedelta(minutes=5),
            calendar=schedule,
        )


def test_calendar_closure_is_not_itself_a_price_imbalance(tmp_path: Path) -> None:
    sessions = tuple(WeeklySession(day, time(0), time(0, 10)) for day in range(7))
    schedule = calendar(sessions=sessions)
    raw = (
        bar(0, 90, 100),
        bar(1, 95, 120),
        bar(2, 95, 110, timestamp=START + timedelta(days=1)),
    )
    result, *_ = run(tmp_path / "closure-no-gap.db", raw, schedule=schedule)
    assert result.events == ()

    actual_gap = (raw[0], raw[1], replace(raw[2], low=101))
    result, *_ = run(tmp_path / "closure-real-gap.db", actual_gap, schedule=schedule)
    assert len(result.events) == 1


def test_calendar_d1_completion_uses_dst_aware_local_days(tmp_path: Path) -> None:
    opens = (
        datetime(2026, 3, 28, tzinfo=timezone.utc),
        datetime(2026, 3, 29, tzinfo=timezone.utc),
        datetime(2026, 3, 29, 23, tzinfo=timezone.utc),
    )
    raw = tuple(
        replace(item, timeframe="D1", timestamp_utc=timestamp)
        for item, timestamp in zip(bullish_bars(), opens)
    )
    cutoff = datetime(2026, 3, 30, 23, tzinfo=timezone.utc)
    schedule = calendar(zone="Europe/London", end=cutoff + timedelta(days=30))
    valid = ValidatedBarSet.from_bars(raw, "D1", cutoff, cutoff_utc=cutoff, calendar=schedule)
    store = SQLitePersistence(tmp_path / "dst.db")
    store.initialize()
    registry = StudyRegistry()
    registry.register(StructureFvgStudy())
    request = StudyRunRequest(STUDY_ID, VERSION, INSTRUMENT, "D1", cutoff, CONFIG)
    result = StudyRunner(registry, store).run(request, valid)
    assert len(result.events) == 1
    assert result.events[0].known_at_utc == cutoff


def test_every_overlapping_triple_has_its_own_stable_event(tmp_path: Path) -> None:
    raw = (bar(0, 7, 10), bar(1, 8, 12), bar(2, 11, 14), bar(3, 13, 15))
    result, *_ = run(tmp_path / "overlap.db", raw)
    assert len(result.events) == 2
    first, second = result.events
    assert isinstance(first, StructureFvgEvent) and isinstance(second, StructureFvgEvent)
    assert first.event_id != second.event_id
    assert (first.first_candle_time_utc, first.third_candle_time_utc) == (
        raw[0].timestamp_utc,
        raw[2].timestamp_utc,
    )
    assert (second.first_candle_time_utc, second.third_candle_time_utc) == (
        raw[1].timestamp_utc,
        raw[3].timestamp_utc,
    )

    adjacent = (
        bar(0, 90, 100),
        bar(1, 95, 300),
        bar(2, 101, 140),
        bar(3, 200, 250),
        bar(4, 90, 150),
        bar(5, 251, 260),
    )
    result, *_ = run(tmp_path / "adjacent.db", adjacent)
    assert len(result.events) == 2
    assert result.events[0].event_id != result.events[1].event_id
    assert result.events[0].third_candle_time_utc == adjacent[2].timestamp_utc
    assert result.events[1].third_candle_time_utc == adjacent[5].timestamp_utc


def test_fewer_than_three_bars_emit_no_events(tmp_path: Path) -> None:
    result, *_ = run(tmp_path / "warmup.db", bullish_bars()[:2])
    assert result.events == ()


def test_future_append_cache_restore_pipeline_and_immutability(tmp_path: Path) -> None:
    raw = (bar(0, 90, 100), bar(1, 95, 120), bar(2, 101, 110), bar(3, 90, 105))
    earlier, store, runner, request, valid = run(tmp_path / "replay.db", raw[:3])
    assert len(earlier.events) == 1
    event = earlier.events[0]
    assert isinstance(event, StructureFvgEvent)
    assert event.source == SOURCE
    assert dict(earlier.provenance)["input_source"] == SOURCE
    assert earlier.data_snapshot_id is not None
    assert runner.run(request, valid) == earlier
    with pytest.raises(FrozenInstanceError):
        event.lower_price = 0

    later, *_ = run(tmp_path / "replay.db", raw)
    assert event in later.events
    assert event.lower_price == 100

    pipeline = HistoricalDataPipeline(MockMarketDataProvider(raw), clock=lambda: valid.cutoff_utc)
    assert run_pipeline_study(runner, pipeline, request, history_days=1) == earlier
    assert store.list_structure_fvg_events(
        STUDY_ID, VERSION, INSTRUMENT, "M5", known_at_or_before_utc=event.known_at_utc
    ) == (event,)


def test_fvg_is_registered_and_rejects_configuration() -> None:
    study = StructureFvgStudy()
    assert study.study_id == STUDY_ID and study.version == VERSION
    assert study.min_bars({}) == 3
    with pytest.raises(StudyConfigError):
        study.validate_config({"threshold": 0})


def test_schema_v11_to_v12_rollback_preservation_and_append_only_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "migration.db"
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 11)
    store = SQLitePersistence(path)
    store.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO market_bars
            (instrument, timeframe, timestamp_utc, open, high, low, close, volume, source)
            VALUES ('GOLD', 'M5', '2026-01-05T00:00:00Z', 95, 100, 90, 95, NULL, 'synthetic')"""
        )
    old_result, *_ = run_sweep(path, sweep_bars())
    assert old_result.events
    preserved_tables = (
        "market_bars",
        "structure_sweep_events",
        "structure_sweep_observations",
        "structure_zones",
        "structure_zone_observations",
    )
    with sqlite3.connect(path) as connection:
        before_schema = connection.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        before_rows = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in preserved_tables
        }

    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 12)
    original = SQLitePersistence._migrate_to_version_12

    def fail_after_migration(
        persistence: SQLitePersistence, connection: sqlite3.Connection
    ) -> None:
        original(persistence, connection)
        raise sqlite3.OperationalError("injected migration failure")

    with monkeypatch.context() as patch:
        patch.setattr(SQLitePersistence, "_migrate_to_version_12", fail_after_migration)
        with pytest.raises(MigrationError, match="rolled back"):
            store.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (11,)
        assert connection.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall() == (before_schema)
        assert all(
            connection.execute(f"SELECT * FROM {table}").fetchall() == rows
            for table, rows in before_rows.items()
        )

    store.initialize()
    result, store, *_ = run(path, bullish_bars())
    assert result.events
    event = result.events[0]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (12,)
        assert all(
            connection.execute(f"SELECT * FROM {table}").fetchall() == rows
            for table, rows in before_rows.items()
        )
        for table in ("structure_fvg_events", "structure_fvg_observations"):
            row = connection.execute(f"SELECT rowid, * FROM {table}").fetchone()
            columns = ", ".join(
                item[1] for item in connection.execute(f"PRAGMA table_info({table})")
            )
            placeholders = ", ".join("?" for _ in row[1:])
            for statement in (
                f"UPDATE {table} SET rowid=rowid",
                f"DELETE FROM {table}",
            ):
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(statement)
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
    assert store.list_structure_fvg_events(STUDY_ID, VERSION, INSTRUMENT, "M5") == (event,)


def test_study_result_and_fvg_event_persistence_roll_back_together(
    tmp_path: Path,
) -> None:
    source_result, *_ = run(tmp_path / "source.db", bullish_bars())

    target_path = tmp_path / "target.db"
    store = SQLitePersistence(target_path)
    store.initialize()

    with sqlite3.connect(target_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_fvg_observation
            BEFORE INSERT ON structure_fvg_observations
            BEGIN
                SELECT RAISE(ABORT, 'injected FVG observation failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected FVG observation failure"):
        store.save_study_result(source_result)

    with sqlite3.connect(target_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_results").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM structure_fvg_events").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM structure_fvg_observations").fetchone() == (
            0,
        )
