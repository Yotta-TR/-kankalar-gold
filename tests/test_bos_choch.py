"""Point-in-time structure-break selection and persistence checks."""

import sqlite3
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
from kankalar_gold_ai.core.contracts import (
    Direction,
    MarketBar,
    StructureBreakEvent,
    StructureBreakType,
    StudyEvent,
    StudyEventConflictError,
    StudyEventType,
    StudyResult,
    StudyStatus,
    structure_break_event_id,
    study_event_id,
)
from kankalar_gold_ai.core.study_results import StudyResultValidationError
from kankalar_gold_ai.data.calendar import CalendarProvenance, SessionCalendar, WeeklySession
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import MigrationError, SQLitePersistence
from kankalar_gold_ai.studies import (
    BosChochStudy,
    ConfirmedSwingsStudy,
    StudyOutput,
    StudyRegistry,
    StudyRequestError,
    StudyRunner,
    StudyRunRequest,
    run_pipeline_study,
)
from tests.fixtures.point_in_time import (
    INSTRUMENT,
    SOURCE,
    assert_no_lookahead,
    blind,
    build_bar,
    end_of,
    run_at,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars(closes: tuple[float, ...], *, wick: float = 1) -> tuple[MarketBar, ...]:
    return tuple(
        MarketBar(
            "GOLD",
            "M5",
            START + timedelta(minutes=5 * i),
            close,
            close + wick,
            max(0.1, close - wick),
            close,
            None,
            "test",
        )
        for i, close in enumerate(closes)
    )


def _swing(
    event_type: StudyEventType, price: float, pivot_minute: int, known_minute: int
) -> StudyEvent:
    pivot = START + timedelta(minutes=pivot_minute)
    return StudyEvent(
        study_event_id("structure.swings", "0.1.0", "GOLD", "M5", event_type, pivot, 1, 1),
        "structure.swings",
        "0.1.0",
        "GOLD",
        "M5",
        event_type,
        pivot,
        START + timedelta(minutes=known_minute),
        price,
        1,
        1,
    )


def _run(
    path: Path,
    bars: tuple[MarketBar, ...],
    *,
    cutoff: datetime | None = None,
) -> tuple[StudyResult, SQLitePersistence, StudyRunner, StudyRunRequest, ValidatedBarSet]:
    end = cutoff or (bars[-1].timestamp_utc + timedelta(minutes=5))
    validated = ValidatedBarSet.from_bars(
        bars, "M5", end, cutoff_utc=end, expected_instrument="GOLD"
    )
    store = SQLitePersistence(path)
    store.initialize()
    registry = StudyRegistry()
    registry.register(BosChochStudy())
    runner = StudyRunner(registry, store)
    request = StudyRunRequest(
        "structure.bos_choch",
        "0.1.0",
        "GOLD",
        "M5",
        end,
        {"left_bars": 1, "right_bars": 1},
    )
    return runner.run(request, validated), store, runner, request, validated


def _inject_swings(monkeypatch: pytest.MonkeyPatch, swings: tuple[StudyEvent, ...]) -> None:
    def compute(
        self: ConfirmedSwingsStudy, bars: object, config: object, context: object
    ) -> StudyOutput:
        return StudyOutput(status=StudyStatus.AVAILABLE, events=swings)

    monkeypatch.setattr(ConfirmedSwingsStudy, "compute_with_context", compute)


@pytest.mark.parametrize(
    ("closes", "event_type", "levels", "expected"),
    [
        ((7, 8, 12, 8, 12), StudyEventType.SWING_HIGH, (9, 11), 9),
        ((13, 12, 8, 12, 8), StudyEventType.SWING_LOW, (11, 9), 11),
    ],
)
def test_multiple_levels_choose_first_crossed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closes: tuple[float, ...],
    event_type: StudyEventType,
    levels: tuple[int, ...],
    expected: int,
) -> None:
    swings = tuple(_swing(event_type, level, -10 + i, 10) for i, level in enumerate(levels))
    _inject_swings(monkeypatch, swings)
    result, *_ = _run(tmp_path / "levels.db", _bars(closes))
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, StructureBreakEvent)
    assert event.broken_level == expected
    assert event.reference_swing_id == next(s.event_id for s in swings if s.price == expected)


def test_nearest_level_does_not_depend_on_rounded_distance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    farther = _swing(StudyEventType.SWING_LOW, 1, -5, 10)
    nearer = _swing(StudyEventType.SWING_LOW, 2, -10, 10)
    assert 1e20 - farther.price == 1e20 - nearer.price
    _inject_swings(monkeypatch, (farther, nearer))
    result, *_ = _run(tmp_path / "rounded.db", _bars((1e20, 1e20, 0.5, 0.5)))
    assert len(result.events) == 1
    assert result.events[0].reference_swing_id == nearer.event_id


def test_equal_levels_choose_latest_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    early = _swing(StudyEventType.SWING_HIGH, 9, -10, 9)
    late = _swing(StudyEventType.SWING_HIGH, 9, -5, 10)
    _inject_swings(monkeypatch, (early, late))
    result, *_ = _run(tmp_path / "ties.db", _bars((7, 8, 11, 11)))
    assert len(result.events) == 1
    assert result.events[0].reference_swing_id == late.event_id


def test_equal_levels_same_confirmation_choose_latest_pivot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = _swing(StudyEventType.SWING_HIGH, 9, -10, 10)
    newer = _swing(StudyEventType.SWING_HIGH, 9, -5, 10)
    _inject_swings(monkeypatch, (older, newer))
    result, *_ = _run(tmp_path / "tie-pivot.db", _bars((7, 8, 11, 11)))
    assert len(result.events) == 1
    assert result.events[0].reference_swing_id == newer.event_id


@pytest.mark.parametrize(
    ("event_type", "previous", "current", "expected"),
    [
        (StudyEventType.SWING_HIGH, 10, 11, True),
        (StudyEventType.SWING_LOW, 10, 9, True),
        (StudyEventType.SWING_HIGH, 9, 10, False),
        (StudyEventType.SWING_LOW, 11, 10, False),
        (StudyEventType.SWING_HIGH, 9, 9.5, False),
        (StudyEventType.SWING_LOW, 11, 10.5, False),
        (StudyEventType.SWING_HIGH, 11, 12, False),
        (StudyEventType.SWING_LOW, 9, 8, False),
    ],
)
def test_close_boundaries_not_wicks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_type: StudyEventType,
    previous: float,
    current: float,
    expected: bool,
) -> None:
    _inject_swings(monkeypatch, (_swing(event_type, 10, -5, 10),))
    result, *_ = _run(
        tmp_path / "boundary.db", _bars((previous, previous, current, current), wick=3)
    )
    assert bool(result.events) is expected


@pytest.mark.parametrize(("known_minute", "eligible"), [(10, True), (12, False), (15, False)])
def test_swing_must_be_known_at_break_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    known_minute: int,
    eligible: bool,
) -> None:
    _inject_swings(monkeypatch, (_swing(StudyEventType.SWING_HIGH, 10, -5, known_minute),))
    result, *_ = _run(tmp_path / "timing.db", _bars((8, 9, 11, 11)))
    assert bool(result.events) is eligible


def test_bias_classification_and_flip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    swings = (
        _swing(StudyEventType.SWING_HIGH, 11, -9, 10),
        _swing(StudyEventType.SWING_HIGH, 13, -8, 10),
        _swing(StudyEventType.SWING_LOW, 9, -7, 10),
        _swing(StudyEventType.SWING_LOW, 8, -6, 10),
        _swing(StudyEventType.SWING_HIGH, 9, -5, 30),
    )
    _inject_swings(monkeypatch, swings)
    result, *_ = _run(tmp_path / "bias.db", _bars((10, 10, 12, 14, 8, 7, 10)))
    assert [event.classification for event in result.events] == [
        StructureBreakType.INITIAL_BREAK,
        StructureBreakType.BOS,
        StructureBreakType.CHOCH,
        StructureBreakType.BOS,
        StructureBreakType.CHOCH,
    ]
    assert [event.direction for event in result.events] == [
        Direction.BULLISH,
        Direction.BULLISH,
        Direction.BEARISH,
        Direction.BEARISH,
        Direction.BULLISH,
    ]
    assert result.direction is Direction.BULLISH


def test_confirmed_swing_close_break_and_cache(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = (
        (6, 7, 5, 6),
        (7, 10, 6, 8),
        (8, 9, 6, 7),
        (7, 12, 6, 11),
    )
    bars = tuple(
        MarketBar("GOLD", "M5", start + timedelta(minutes=5 * i), *row, None, "test")
        for i, row in enumerate(rows)
    )
    cutoff = start + timedelta(minutes=20)
    validated = ValidatedBarSet.from_bars(
        bars, "M5", cutoff, cutoff_utc=cutoff, expected_instrument="GOLD"
    )
    store = SQLitePersistence(tmp_path / "events.db")
    store.initialize()
    registry = StudyRegistry()
    registry.register(BosChochStudy())
    runner = StudyRunner(registry, store)
    request = StudyRunRequest(
        "structure.bos_choch",
        "0.1.0",
        "GOLD",
        "M5",
        cutoff,
        {"left_bars": 1, "right_bars": 1},
    )
    result = runner.run(request, validated)
    assert result.status is StudyStatus.AVAILABLE
    assert len(result.events) == 1
    event = result.events[0]
    assert event.direction is Direction.BULLISH
    assert event.classification is StructureBreakType.INITIAL_BREAK
    assert event.broken_level == 10
    assert event.break_bar_time_utc == start + timedelta(minutes=15)
    assert event.known_at_utc == cutoff
    assert runner.run(request, validated) == result


def _real_bars() -> tuple[MarketBar, ...]:
    rows = ((6, 7, 5, 6), (7, 10, 6, 8), (8, 9, 6, 7), (7, 12, 6, 11))
    return tuple(
        MarketBar("GOLD", "M5", START + timedelta(minutes=5 * i), *row, None, "test")
        for i, row in enumerate(rows)
    )


def test_replay_future_mutation_and_pipeline(tmp_path: Path) -> None:
    raw = _real_bars()
    initial, store, _runner, request, valid = _run(tmp_path / "replay.db", raw)
    assert len(initial.events) == 1
    event = initial.events[0]
    assert isinstance(event, StructureBreakEvent)
    assert store.list_structure_break_events("structure.bos_choch", "0.1.0", "GOLD", "M5") == (
        event,
    )
    assert not store.list_structure_break_events(
        "structure.bos_choch",
        "0.1.0",
        "GOLD",
        "M5",
        known_at_or_before_utc=event.known_at_utc - timedelta(microseconds=1),
    )
    assert store.list_structure_break_events(
        "structure.bos_choch",
        "0.1.0",
        "GOLD",
        "M5",
        known_at_or_before_utc=event.known_at_utc,
    ) == (event,)
    reopened = SQLitePersistence(tmp_path / "replay.db")
    registry = StudyRegistry()
    registry.register(BosChochStudy())
    assert StudyRunner(registry, reopened).run(request, valid) == initial
    pipeline = HistoricalDataPipeline(MockMarketDataProvider(raw), clock=lambda: valid.cutoff_utc)
    pipeline_store = SQLitePersistence(tmp_path / "pipeline.db")
    pipeline_store.initialize()
    assert (
        run_pipeline_study(StudyRunner(registry, pipeline_store), pipeline, request, history_days=1)
        == initial
    )

    for future in (
        MarketBar("GOLD", "M5", START + timedelta(minutes=20), 11, 12, 10, 11, None, "test"),
        MarketBar("GOLD", "M5", START + timedelta(minutes=20), 11, 100, 1, 50, None, "test"),
    ):
        later, *_ = _run(tmp_path / "replay.db", (*raw, future))
        assert event in later.events


def test_no_lookahead_harness_on_nonempty_events() -> None:
    prefix = tuple(replace(bar, instrument=INSTRUMENT, source=SOURCE) for bar in _real_bars())
    universe = prefix + tuple(build_bar(i, "M5", 12 + i % 3) for i in range(4, 12))
    config = {"left_bars": 1, "right_bars": 1}
    assert run_at(BosChochStudy(), universe, "M5", end_of(3), config=config).events
    report = assert_no_lookahead(
        blind(BosChochStudy), universe, "M5", [end_of(3), end_of(5), end_of(7)], config=config
    )
    assert report.cutoffs_checked == 3


def test_cutoff_rejects_uncompleted_bar_and_mismatched_request(tmp_path: Path) -> None:
    raw = _real_bars()
    cutoff = START + timedelta(minutes=19, seconds=59)
    with pytest.raises(ValueError, match="complete after cutoff"):
        ValidatedBarSet.from_bars(
            raw, "M5", cutoff + timedelta(seconds=1), cutoff_utc=cutoff, expected_instrument="GOLD"
        )
    result, _store, runner, request, valid = _run(tmp_path / "cutoff.db", raw)
    assert result.events
    with pytest.raises(StudyRequestError, match="cutoff"):
        runner.run(replace(request, as_of_utc=cutoff), valid)


@pytest.mark.parametrize(
    ("opens", "cutoff"),
    [
        (
            (
                datetime(2026, 3, 27, tzinfo=timezone.utc),
                datetime(2026, 3, 28, tzinfo=timezone.utc),
                datetime(2026, 3, 29, tzinfo=timezone.utc),
                datetime(2026, 3, 29, 23, tzinfo=timezone.utc),
            ),
            datetime(2026, 3, 30, 23, tzinfo=timezone.utc),
        ),
        (
            (
                datetime(2026, 10, 22, 23, tzinfo=timezone.utc),
                datetime(2026, 10, 23, 23, tzinfo=timezone.utc),
                datetime(2026, 10, 24, 23, tzinfo=timezone.utc),
                datetime(2026, 10, 26, tzinfo=timezone.utc),
            ),
            datetime(2026, 10, 27, tzinfo=timezone.utc),
        ),
    ],
)
def test_d1_break_completion_and_calendar_context_across_dst(
    tmp_path: Path,
    opens: tuple[datetime, ...],
    cutoff: datetime,
) -> None:
    rows = ((6, 7, 5, 6), (7, 10, 6, 8), (8, 9, 6, 7), (7, 12, 6, 11))
    raw = tuple(MarketBar("GOLD", "D1", t, *row, None, "test") for t, row in zip(opens, rows))
    calendar = SessionCalendar(
        calendar_id="synthetic",
        version="1",
        provider_id="test",
        instrument_id="GOLD",
        provenance=CalendarProvenance(source="synthetic", reference="test_bos_choch"),
        timezone_name="Europe/London",
        effective_start_utc=opens[0] - timedelta(days=2),
        effective_end_utc=cutoff + timedelta(days=2),
        coverage_start_utc=opens[0] - timedelta(days=2),
        coverage_end_utc=cutoff + timedelta(days=2),
        weekly_sessions=tuple(
            WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(7)
        ),
    )
    valid = ValidatedBarSet.from_bars(
        raw, "D1", cutoff, cutoff_utc=cutoff, expected_instrument="GOLD", calendar=calendar
    )
    store = SQLitePersistence(tmp_path / "dst.db")
    store.initialize()
    registry = StudyRegistry()
    registry.register(BosChochStudy())
    runner = StudyRunner(registry, store)
    request = StudyRunRequest(
        "structure.bos_choch",
        "0.1.0",
        "GOLD",
        "D1",
        cutoff,
        {"left_bars": 1, "right_bars": 1},
    )
    result = runner.run(request, valid)
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, StructureBreakEvent)
    assert event.break_bar_time_utc == opens[3]
    assert event.known_at_utc == cutoff
    assert event.calendar_context_id == valid.calendar_context_id
    assert runner.run(request, valid) == result


def test_structure_break_immutable_conflicts_and_sql_guards(tmp_path: Path) -> None:
    result, store, *_ = _run(tmp_path / "guards.db", _real_bars())
    event = result.events[0]
    assert isinstance(event, StructureBreakEvent)
    with sqlite3.connect(tmp_path / "guards.db") as connection:
        before = connection.execute("SELECT COUNT(*) FROM study_results").fetchone()
    with pytest.raises(StudyEventConflictError):
        store.save_study_result(replace(result, events=(replace(event, broken_level=11),)))
    with sqlite3.connect(tmp_path / "guards.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_results").fetchone() == before
        for table, count in (("structure_break_events", 12), ("structure_break_observations", 2)):
            row = connection.execute(f"SELECT * FROM {table}").fetchone()
            assert row is not None
            for operation in (f"UPDATE {table} SET event_id = event_id", f"DELETE FROM {table}"):
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(operation)
            for recursive in (0, 1):
                connection.execute(f"PRAGMA recursive_triggers = {recursive}")
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    placeholders = ", ".join("?" for _ in range(count))
                    connection.execute(
                        f"INSERT OR REPLACE INTO {table} VALUES ({placeholders})",
                        row,
                    )
                rowid = connection.execute(f"SELECT rowid FROM {table}").fetchone()[0]
                changed_row = list(row)
                changed_row[0 if count == 12 else 1] = "different-event"
                columns = ", ".join(
                    info[1] for info in connection.execute(f"PRAGMA table_info({table})")
                )
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(
                        f"INSERT OR REPLACE INTO {table} (rowid) VALUES (?)", (rowid,)
                    )
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    connection.execute(
                        f"INSERT OR REPLACE INTO {table} (rowid, {columns}) "
                        f"VALUES (?, {placeholders})",
                        (rowid, *changed_row),
                    )
            assert connection.execute(f"SELECT * FROM {table}").fetchone() == row

    other_context = "calendar-context-v1:sha256:" + "f" * 64
    changed = replace(
        event,
        calendar_context_id=other_context,
        event_id=structure_break_event_id(
            event.study_id,
            event.version,
            event.instrument,
            event.timeframe,
            event.reference_swing_id,
            event.break_bar_time_utc,
            other_context,
        ),
    )
    with pytest.raises(StudyResultValidationError, match="calendar context"):
        store.save_study_result(replace(result, events=(changed,)))


def test_v8_migration_preserves_v7_rows_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "migration.db"
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 7)
    store = SQLitePersistence(path)
    store.initialize()
    raw = _real_bars()
    cutoff = START + timedelta(minutes=20)
    valid = ValidatedBarSet.from_bars(
        raw, "M5", cutoff, cutoff_utc=cutoff, expected_instrument="GOLD"
    )
    registry = StudyRegistry()
    registry.register(ConfirmedSwingsStudy())
    request = StudyRunRequest(
        "structure.swings",
        "0.1.0",
        "GOLD",
        "M5",
        cutoff,
        {"left_bars": 1, "right_bars": 1},
    )
    old_result = StudyRunner(registry, store).run(request, valid)
    assert old_result.events
    with sqlite3.connect(path) as connection:
        before = connection.execute("SELECT * FROM study_events").fetchall()
        before_results = connection.execute("SELECT * FROM study_results").fetchall()
        before_links = connection.execute("SELECT * FROM study_event_observations").fetchall()
        before_triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        ).fetchall()
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 8)
    original = SQLitePersistence._migrate_to_version_8
    with monkeypatch.context() as patch:

        def fail(store: SQLitePersistence, connection: sqlite3.Connection) -> None:
            original(store, connection)
            raise sqlite3.OperationalError("injected failure")

        patch.setattr(SQLitePersistence, "_migrate_to_version_8", fail)
        with pytest.raises(MigrationError, match="rolled back"):
            SQLitePersistence(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (7,)
        assert connection.execute("SELECT * FROM study_events").fetchall() == before
        assert connection.execute("SELECT * FROM study_results").fetchall() == before_results
        assert (
            connection.execute("SELECT * FROM study_event_observations").fetchall() == before_links
        )
        assert (
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
            ).fetchall()
            == before_triggers
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='structure_break_events'"
            ).fetchone()
            is None
        )
    SQLitePersistence(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (8,)
        assert connection.execute("SELECT * FROM study_events").fetchall() == before
        assert connection.execute("SELECT * FROM study_results").fetchall() == before_results
        assert (
            connection.execute("SELECT * FROM study_event_observations").fetchall() == before_links
        )
        for table in ("study_events", "study_event_observations"):
            record = connection.execute(f"SELECT rowid, * FROM {table} LIMIT 1").fetchone()
            assert record is not None
            rowid, *values = record
            columns = ", ".join(
                item[1] for item in connection.execute(f"PRAGMA table_info({table})")
            )
            placeholders = ", ".join("?" for _ in values)
            values[0 if table == "study_events" else 1] = "different-event"
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(
                    f"INSERT OR REPLACE INTO {table} (rowid, {columns}) VALUES (?, {placeholders})",
                    (rowid, *values),
                )
    assert StudyRunner(registry, store).run(request, valid) == old_result
