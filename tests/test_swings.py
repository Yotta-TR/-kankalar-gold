"""Confirmed-swing math, cutoff, cache, migration, and append-only tests."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
from kankalar_gold_ai.core.contracts import (
    MarketBar,
    StudyEventConflictError,
    StudyEventType,
    StudyResult,
    StudyStatus,
    study_event_id,
)
from kankalar_gold_ai.data.calendar import (
    CalendarProvenance,
    SessionCalendar,
    WeeklySession,
)
from kankalar_gold_ai.data.errors import HistoricalDataQualityError
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.data.quality import TIMEFRAME_DURATIONS, bar_completion_utc
from kankalar_gold_ai.data.snapshot import compute_bar_snapshot_id
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import MigrationError, SQLitePersistence
from kankalar_gold_ai.studies import (
    ConfirmedSwingsStudy,
    StudyConfigError,
    StudyRegistry,
    StudyRequestError,
    StudyRunner,
    StudyRunRequest,
    run_pipeline_study,
)

UTC = timezone.utc
BASE = datetime(2026, 1, 1, tzinfo=UTC)
STUDY_ID = "structure.swings"
VERSION = "0.1.0"
INSTRUMENT = "TEST_GOLD"
SOURCE = "synthetic-swings"


def _raw_bars(
    highs: tuple[float, ...],
    lows: tuple[float, ...],
    *,
    timeframe: str = "M5",
    opens: tuple[datetime, ...] | None = None,
) -> tuple[MarketBar, ...]:
    duration = TIMEFRAME_DURATIONS[timeframe]
    timestamps = opens or tuple(BASE + duration * index for index in range(len(highs)))
    assert len(highs) == len(lows) == len(timestamps)
    bars: list[MarketBar] = []
    for timestamp, high, low in zip(timestamps, highs, lows):
        middle = (high + low) / 2
        bars.append(
            MarketBar(
                instrument=INSTRUMENT,
                timeframe=timeframe,
                timestamp_utc=timestamp,
                open=middle,
                high=high,
                low=low,
                close=middle,
                volume=None,
                source=SOURCE,
            )
        )
    return tuple(bars)


def _calendar(
    timezone_name: str,
    start: datetime,
    end: datetime,
) -> SessionCalendar:
    return SessionCalendar(
        calendar_id="synthetic.swings",
        version="1",
        provider_id=SOURCE,
        instrument_id=INSTRUMENT,
        provenance=CalendarProvenance(
            source="synthetic-test-fixture",
            reference="tests/test_swings.py",
        ),
        timezone_name=timezone_name,
        effective_start_utc=start,
        effective_end_utc=end,
        coverage_start_utc=start,
        coverage_end_utc=end,
        weekly_sessions=tuple(
            WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(7)
        ),
    )


def _validated(
    bars: tuple[MarketBar, ...],
    *,
    calendar: SessionCalendar | None = None,
) -> ValidatedBarSet:
    cutoff = bar_completion_utc(
        bars[-1].timestamp_utc,
        bars[-1].timeframe,
        calendar,
    )
    return ValidatedBarSet.from_bars(
        bars,
        bars[-1].timeframe,
        cutoff,
        cutoff_utc=cutoff,
        expected_instrument=INSTRUMENT,
        calendar=calendar,
    )


def _runner(path: Path) -> tuple[StudyRunner, SQLitePersistence]:
    store = SQLitePersistence(path)
    store.initialize()
    registry = StudyRegistry()
    registry.register(ConfirmedSwingsStudy())
    return StudyRunner(registry, store), store


def _request(
    bars: ValidatedBarSet,
    *,
    left: object = 1,
    right: object = 1,
) -> StudyRunRequest:
    return StudyRunRequest(
        study_id=STUDY_ID,
        version=VERSION,
        instrument=INSTRUMENT,
        timeframe=bars.timeframe,
        as_of_utc=bars.cutoff_utc,
        config={"left_bars": left, "right_bars": right},  # type: ignore[dict-item]
    )


def _run(
    path: Path,
    highs: tuple[float, ...],
    lows: tuple[float, ...],
    *,
    left: int = 1,
    right: int = 1,
    timeframe: str = "M5",
    opens: tuple[datetime, ...] | None = None,
    calendar: SessionCalendar | None = None,
) -> tuple[StudyResult, SQLitePersistence, ValidatedBarSet]:
    bars = _validated(_raw_bars(highs, lows, timeframe=timeframe, opens=opens), calendar=calendar)
    runner, store = _runner(path)
    return runner.run(_request(bars, left=left, right=right), bars), store, bars


def test_registry_and_supported_timeframes() -> None:
    registry = StudyRegistry()
    registered = registry.register(ConfirmedSwingsStudy())
    assert registered.study_id == STUDY_ID
    assert registered.version == VERSION
    assert registered.required_timeframes == {"M1", "M5", "M15", "H1", "H4", "D1"}


@pytest.mark.parametrize("timeframe", ("M1", "M5", "M15", "H1", "H4", "D1"))
def test_study_executes_independently_on_each_supported_timeframe(
    tmp_path: Path, timeframe: str
) -> None:
    result, _store, _bars = _run(
        tmp_path / f"{timeframe}.db",
        (5, 9, 6),
        (2, 3, 4),
        timeframe=timeframe,
    )
    assert result.status is StudyStatus.AVAILABLE
    assert len(result.events) == 1


def test_right_window_confirmation_waits_for_exact_number_of_bars(tmp_path: Path) -> None:
    highs = (4, 9, 5, 6)
    lows = (2, 3, 4, 5)
    before_last_right_bar = _validated(_raw_bars(highs[:3], lows[:3]))
    runner, _store = _runner(tmp_path / "right-delay.db")
    before = runner.run(_request(before_last_right_bar, right=2), before_last_right_bar)
    assert before.status is StudyStatus.INSUFFICIENT_DATA
    assert before.events == ()

    completed = _validated(_raw_bars(highs, lows))
    result = runner.run(_request(completed, right=2), completed)
    assert len(result.events) == 1
    assert result.events[0].pivot_time_utc == BASE + timedelta(minutes=5)
    assert result.events[0].known_at_utc == BASE + timedelta(minutes=20)


def test_runner_rejects_raw_bars_outside_validated_carrier(tmp_path: Path) -> None:
    raw = _raw_bars((5, 9, 6), (2, 3, 4))
    validated = _validated(raw)
    runner, _store = _runner(tmp_path / "raw.db")
    with pytest.raises(StudyRequestError, match="ValidatedBarSet"):
        runner.run(_request(validated), raw)  # type: ignore[arg-type]


def test_high_low_multiple_events_and_outside_candle(tmp_path: Path) -> None:
    result, store, _bars = _run(
        tmp_path / "math.db",
        (4, 5, 3, 6, 4, 5, 4),
        (3, 2, 2.5, 1, 2, 1, 3),
    )
    assert result.status is StudyStatus.AVAILABLE
    assert result.value is None and result.normalized_value is None
    assert result.state == "SWINGS_CONFIRMED"
    assert [event.event_type for event in result.events] == [
        StudyEventType.SWING_HIGH,
        StudyEventType.SWING_LOW,
        StudyEventType.SWING_HIGH,
        StudyEventType.SWING_LOW,
        StudyEventType.SWING_HIGH,
        StudyEventType.SWING_LOW,
    ]
    assert [event.price for event in result.events] == [5, 2, 6, 1, 5, 1]
    assert result.events[0].pivot_time_utc == result.events[1].pivot_time_utc
    assert store.list_study_events(STUDY_ID, VERSION, INSTRUMENT, "M5") == result.events


def test_exact_boundaries_ties_and_plateau_are_rejected(tmp_path: Path) -> None:
    for index, highs in enumerate(((5, 5, 4), (4, 5, 5), (2, 5, 5, 2))):
        lows = tuple(0.5 for _ in highs)
        result, _store, _bars = _run(tmp_path / f"tie-{index}.db", highs, lows)
        assert not any(event.event_type is StudyEventType.SWING_HIGH for event in result.events)
    for index, lows in enumerate(((2, 2, 3), (3, 2, 2), (5, 1, 1, 5))):
        highs = tuple(10.0 for _ in lows)
        result, _store, _bars = _run(tmp_path / f"low-tie-{index}.db", highs, lows)
        assert not any(event.event_type is StudyEventType.SWING_LOW for event in result.events)


def test_warmup_and_empty_result_states(tmp_path: Path) -> None:
    short, _store, _ = _run(tmp_path / "short.db", (1, 5), (0.5, 1))
    assert short.status is StudyStatus.INSUFFICIENT_DATA
    assert short.events == ()
    exact, _store, _ = _run(tmp_path / "exact.db", (2, 5, 3), (0.5, 1, 0.75))
    assert exact.status is StudyStatus.AVAILABLE
    assert exact.state == "SWINGS_CONFIRMED"
    empty, _store, _ = _run(tmp_path / "empty.db", (2, 3, 4), (0.5, 1, 2))
    assert empty.status is StudyStatus.AVAILABLE
    assert empty.state == "NO_CONFIRMED_SWINGS"
    assert empty.events == ()


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"left_bars": 1},
        {"right_bars": 1},
        {"left_bars": 1, "right_bars": 1, "other": 1},
        {"left_bars": True, "right_bars": 1},
        {"left_bars": 1.0, "right_bars": 1},
        {"left_bars": "1", "right_bars": 1},
        {"left_bars": 0, "right_bars": 1},
        {"left_bars": -1, "right_bars": 1},
        {"left_bars": 1, "right_bars": False},
        {"left_bars": 1, "right_bars": 2.0},
        {"left_bars": 1, "right_bars": "2"},
        {"left_bars": 1, "right_bars": 0},
        {"left_bars": 1, "right_bars": -2},
    ],
)
def test_invalid_configuration_is_rejected(tmp_path: Path, config: dict[str, object]) -> None:
    bars = _validated(_raw_bars((2, 3, 4), (0.5, 1, 2)))
    runner, _store = _runner(tmp_path / "bad-config.db")
    request = StudyRunRequest(
        study_id=STUDY_ID,
        version=VERSION,
        instrument=INSTRUMENT,
        timeframe="M5",
        as_of_utc=bars.cutoff_utc,
        config=config,  # type: ignore[arg-type]
    )
    with pytest.raises(StudyConfigError):
        runner.run(request, bars)


def test_non_finite_ohlc_is_rejected_before_study_execution() -> None:
    bars = _raw_bars((1.0, float("nan"), 3.0), (0.5, 1.0, 2.0))
    with pytest.raises(HistoricalDataQualityError):
        _validated(bars)


def test_inconsistent_ohlc_is_rejected_before_study_execution() -> None:
    with pytest.raises(ValueError, match="bar OHLC values are inconsistent"):
        MarketBar(
            instrument=INSTRUMENT,
            timeframe="M5",
            timestamp_utc=BASE,
            open=5,
            high=4,
            low=3,
            close=4,
            volume=None,
            source=SOURCE,
        )


def test_confirmation_time_and_future_append_preserve_event_identity(tmp_path: Path) -> None:
    prefix, _store, validated = _run(
        tmp_path / "prefix.db",
        (5, 9, 6),
        (2, 3, 4),
        right=1,
    )
    assert len(prefix.events) == 1
    event = prefix.events[0]
    assert event.pivot_time_utc == BASE + timedelta(minutes=5)
    assert event.known_at_utc == BASE + timedelta(minutes=15)
    assert event.known_at_utc <= validated.cutoff_utc

    before_confirmation_bars = _validated(_raw_bars((5, 9), (2, 3)))
    runner, _store = _runner(tmp_path / "before.db")
    before = runner.run(_request(before_confirmation_bars), before_confirmation_bars)
    assert before.status is StudyStatus.INSUFFICIENT_DATA
    assert before.events == ()

    later, _store, _ = _run(
        tmp_path / "later.db",
        (5, 9, 6, 100, 6),
        (2, 3, 4, 1, 5),
        right=1,
    )
    matching = next(item for item in later.events if item.event_id == event.event_id)
    assert matching == event


def test_mutating_bars_after_confirmation_does_not_change_known_event(tmp_path: Path) -> None:
    original, _store, _bars = _run(
        tmp_path / "before-future-mutation.db",
        (5, 9, 6, 7, 8),
        (2, 3, 4, 5, 6),
        right=1,
    )
    changed_future, _store, _bars = _run(
        tmp_path / "after-future-mutation.db",
        (5, 9, 6, 100, 101),
        (2, 3, 4, 0.1, 0.2),
        right=1,
    )

    pivot_time = BASE + timedelta(minutes=5)
    original_event = next(event for event in original.events if event.pivot_time_utc == pivot_time)
    mutated_event = next(
        event for event in changed_future.events if event.pivot_time_utc == pivot_time
    )
    assert original_event.known_at_utc == BASE + timedelta(minutes=15)
    assert mutated_event == original_event


@pytest.mark.parametrize(
    ("timezone_name", "opens", "expected_known_at"),
    [
        (
            "Europe/London",
            (
                datetime(2026, 3, 27, 0, tzinfo=UTC),
                datetime(2026, 3, 28, 0, tzinfo=UTC),
                datetime(2026, 3, 29, 0, tzinfo=UTC),
            ),
            datetime(2026, 3, 29, 23, tzinfo=UTC),
        ),
        (
            "Europe/London",
            (
                datetime(2026, 10, 22, 23, tzinfo=UTC),
                datetime(2026, 10, 23, 23, tzinfo=UTC),
                datetime(2026, 10, 24, 23, tzinfo=UTC),
            ),
            datetime(2026, 10, 26, 0, tzinfo=UTC),
        ),
        (
            "UTC",
            (
                datetime(2026, 1, 1, 0, tzinfo=UTC),
                datetime(2026, 1, 2, 0, tzinfo=UTC),
                datetime(2026, 1, 3, 0, tzinfo=UTC),
            ),
            datetime(2026, 1, 4, 0, tzinfo=UTC),
        ),
    ],
)
def test_d1_confirmation_uses_calendar_day_completion(
    tmp_path: Path,
    timezone_name: str,
    opens: tuple[datetime, ...],
    expected_known_at: datetime,
) -> None:
    schedule = _calendar(
        timezone_name,
        opens[0] - timedelta(days=2),
        expected_known_at + timedelta(days=2),
    )
    result, _store, _bars = _run(
        tmp_path / f"d1-{expected_known_at.isoformat().replace(':', '-')}.db",
        (6, 9, 7),
        (5, 1, 4),
        timeframe="D1",
        opens=opens,
        calendar=schedule,
    )
    event = next(item for item in result.events if item.event_type is StudyEventType.SWING_HIGH)
    assert event.known_at_utc == expected_known_at


def test_d1_without_calendar_uses_fixed_24_hour_utc_completion(tmp_path: Path) -> None:
    result, _store, bars = _run(
        tmp_path / "d1-no-calendar.db",
        (6, 9, 7),
        (5, 1, 4),
        timeframe="D1",
        opens=(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            datetime(2026, 1, 3, tzinfo=UTC),
        ),
    )

    assert result.events[0].known_at_utc == datetime(2026, 1, 4, tzinfo=UTC)
    assert '"completion_policy":"fixed-24-hour-utc"' in bars.calendar_context_json


def test_d1_calendar_context_separates_cache_results(tmp_path: Path) -> None:
    opens = tuple(datetime(2026, 3, 27 + index, tzinfo=UTC) for index in range(3))
    raw = _raw_bars((6, 9, 7), (5, 1, 4), timeframe="D1", opens=opens)
    as_of = datetime(2026, 3, 30, 1, tzinfo=UTC)
    utc_calendar = _calendar(
        "UTC",
        opens[0] - timedelta(days=2),
        as_of + timedelta(days=2),
    )
    london_calendar = _calendar(
        "Europe/London",
        opens[0] - timedelta(days=2),
        as_of + timedelta(days=2),
    )
    validated_by_timezone = {
        calendar.timezone_name: ValidatedBarSet.from_bars(
            raw,
            "D1",
            as_of,
            cutoff_utc=as_of,
            expected_instrument=INSTRUMENT,
            calendar=calendar,
        )
        for calendar in (utc_calendar, london_calendar)
    }
    utc_bars = validated_by_timezone["UTC"]
    london_bars = validated_by_timezone["Europe/London"]
    assert utc_bars.bars == london_bars.bars
    assert utc_bars.cutoff_utc == london_bars.cutoff_utc == as_of
    assert utc_bars.calendar_context_id != london_bars.calendar_context_id

    runner, store = _runner(tmp_path / "calendar-cache.db")
    utc_result = runner.run(_request(utc_bars), utc_bars)
    london_result = runner.run(_request(london_bars), london_bars)

    assert utc_result.inputs == london_result.inputs
    assert utc_result.as_of_utc == london_result.as_of_utc == as_of
    assert (
        utc_result.data_snapshot_id
        == london_result.data_snapshot_id
        == (compute_bar_snapshot_id(raw))
    )
    assert utc_result.events[0].known_at_utc == datetime(2026, 3, 30, 0, tzinfo=UTC)
    assert london_result.events[0].known_at_utc == datetime(2026, 3, 29, 23, tzinfo=UTC)
    assert dict(utc_result.provenance)["calendar_context_id"] == utc_bars.calendar_context_id
    assert dict(london_result.provenance)["calendar_context_id"] == london_bars.calendar_context_id
    assert '"timezone":"UTC"' in dict(utc_result.provenance)["calendar_context_json"]
    assert '"timezone":"Europe/London"' in dict(london_result.provenance)["calendar_context_json"]
    with sqlite3.connect(tmp_path / "calendar-cache.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_results").fetchone() == (2,)


def test_d1_same_calendar_replay_uses_cached_result(tmp_path: Path) -> None:
    opens = tuple(datetime(2026, 3, 27 + index, tzinfo=UTC) for index in range(3))
    raw = _raw_bars((6, 9, 7), (5, 1, 4), timeframe="D1", opens=opens)
    as_of = datetime(2026, 3, 30, 1, tzinfo=UTC)
    calendar = _calendar(
        "Europe/London",
        opens[0] - timedelta(days=2),
        as_of + timedelta(days=2),
    )
    bars = ValidatedBarSet.from_bars(
        raw,
        "D1",
        as_of,
        cutoff_utc=as_of,
        expected_instrument=INSTRUMENT,
        calendar=calendar,
    )
    runner, _store = _runner(tmp_path / "same-calendar-cache.db")
    first = runner.run(_request(bars), bars)
    replay_runner, _reopened_store = _runner(tmp_path / "same-calendar-cache.db")
    replay = replay_runner.run(_request(bars), bars)

    assert replay == first
    with sqlite3.connect(tmp_path / "same-calendar-cache.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_results").fetchone() == (1,)


def test_cache_persists_structured_events_and_exposes_append_only_retrieval(
    tmp_path: Path,
) -> None:
    result, store, bars = _run(
        tmp_path / "cache.db",
        (5, 9, 6),
        (2, 3, 4),
    )
    registry = StudyRegistry()
    registry.register(ConfirmedSwingsStudy())
    runner = StudyRunner(registry, store)
    cached = runner.run(_request(bars), bars)
    assert cached == result
    assert store.list_study_events(STUDY_ID, VERSION, INSTRUMENT, "M5") == result.events
    assert (
        store.list_study_events(
            STUDY_ID,
            VERSION,
            INSTRUMENT,
            "M5",
            known_at_or_before_utc=result.events[0].pivot_time_utc,
        )
        == ()
    )


def test_empty_event_collection_is_cached_without_becoming_a_data_error(
    tmp_path: Path,
) -> None:
    result, store, bars = _run(
        tmp_path / "empty-cache.db",
        (2, 3, 4),
        (0.5, 1, 2),
    )
    registry = StudyRegistry()
    registry.register(ConfirmedSwingsStudy())
    cached = StudyRunner(registry, store).run(_request(bars), bars)
    assert result.status is StudyStatus.AVAILABLE
    assert result.state == "NO_CONFIRMED_SWINGS"
    assert cached == result and cached.events == ()
    assert store.list_study_events(STUDY_ID, VERSION, INSTRUMENT, "M5") == ()


def test_event_retrieval_cutoff_compares_subsecond_timestamps_exactly(
    tmp_path: Path,
) -> None:
    opens = tuple(
        BASE + timedelta(microseconds=500_000) + timedelta(minutes=5 * index) for index in range(3)
    )
    result, store, _bars = _run(
        tmp_path / "subsecond.db",
        (5, 9, 6),
        (2, 3, 4),
        opens=opens,
    )
    assert result.events[0].known_at_utc == BASE + timedelta(minutes=15, microseconds=500_000)
    assert (
        store.list_study_events(
            STUDY_ID,
            VERSION,
            INSTRUMENT,
            "M5",
            known_at_or_before_utc=BASE + timedelta(minutes=15),
        )
        == ()
    )


def test_repeated_event_is_idempotent_and_conflict_rolls_back_parent_result(
    tmp_path: Path,
) -> None:
    result, store, _bars = _run(
        tmp_path / "persist.db",
        (5, 9, 6),
        (2, 3, 4),
    )
    store.save_study_result(result)
    with sqlite3.connect(tmp_path / "persist.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_events").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM study_event_observations").fetchone() == (
            2,
        )
        observed_snapshots = connection.execute(
            """
            SELECT r.data_snapshot_id
            FROM study_event_observations o
            JOIN study_results r ON r.id = o.result_id
            ORDER BY r.id
            """
        ).fetchall()
        assert observed_snapshots == [
            (result.data_snapshot_id,),
            (result.data_snapshot_id,),
        ]
        before_results = connection.execute("SELECT COUNT(*) FROM study_results").fetchone()

    conflict = result.events[0]
    changed = replace(conflict, price=conflict.price + 1)
    new_event = replace(
        conflict,
        event_id=study_event_id(
            STUDY_ID,
            VERSION,
            INSTRUMENT,
            "M5",
            StudyEventType.SWING_LOW,
            conflict.pivot_time_utc,
            conflict.left_bars,
            conflict.right_bars,
        ),
        event_type=StudyEventType.SWING_LOW,
        price=2,
    )
    with pytest.raises(StudyEventConflictError, match="conflicting immutable payload"):
        store.save_study_result(replace(result, events=(new_event, changed)))
    with sqlite3.connect(tmp_path / "persist.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_results").fetchone() == before_results
        assert connection.execute("SELECT COUNT(*) FROM study_events").fetchone() == (1,)
        assert connection.execute("SELECT price FROM study_events").fetchone() == (conflict.price,)


def test_pipeline_bridge_executes_swings_from_validated_bars(tmp_path: Path) -> None:
    raw = _raw_bars((5, 9, 6), (2, 3, 4))
    validated = _validated(raw)
    registry = StudyRegistry()
    registry.register(ConfirmedSwingsStudy())
    store = SQLitePersistence(tmp_path / "pipeline.db")
    store.initialize()
    runner = StudyRunner(registry, store)
    pipeline = HistoricalDataPipeline(
        MockMarketDataProvider(raw), clock=lambda: validated.cutoff_utc
    )
    request = _request(validated)
    result = run_pipeline_study(runner, pipeline, request, history_days=1)
    assert result.status is StudyStatus.AVAILABLE
    assert len(result.events) == 1


def test_v4_migration_adds_events_without_rewriting_existing_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "v4.db"
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 4)
    store = SQLitePersistence(path)
    store.initialize()
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 5)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO study_results (
                study_id, version, instrument, timeframe, as_of_utc, status, state,
                direction, inputs_json, evidence_json, limitations_json,
                provenance_json, cache_format_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "trend.old",
                "1.0.0",
                INSTRUMENT,
                "M5",
                "2026-01-01T00:15:00Z",
                "AVAILABLE",
                "old",
                "NEUTRAL",
                "{}",
                "[]",
                "[]",
                '{"cache_format_version":"study-result-v1","input_bar_count":"3",'
                '"input_source":"synthetic","input_window_end_utc":"2026-01-01T00:15:00Z",'
                '"input_window_start_utc":"2026-01-01T00:00:00Z","study_version":"1.0.0"}',
                "study-result-v1",
            ),
        )
        before = connection.execute("SELECT * FROM study_results").fetchall()

    SQLitePersistence(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (5,)
        assert connection.execute("SELECT * FROM study_results").fetchall() == before
        assert connection.execute("SELECT COUNT(*) FROM study_events").fetchone() == (0,)


def test_v5_migration_adds_calendar_cache_identity_as_null_for_old_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "v5.db"
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 5)
    store = SQLitePersistence(path)
    store.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO study_results (
                study_id, version, instrument, timeframe, as_of_utc, status, direction,
                inputs_json, evidence_json, limitations_json, data_snapshot_id,
                provenance_json, cache_format_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                STUDY_ID,
                VERSION,
                INSTRUMENT,
                "D1",
                "2026-03-30T01:00:00Z",
                "AVAILABLE",
                "NOT_APPLICABLE",
                '{"left_bars": 1, "right_bars": 1}',
                "[]",
                "[]",
                "bars-v1:sha256:" + "a" * 64,
                '{"cache_format_version":"study-result-v1"}',
                "study-result-v1",
            ),
        )

    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 6)
    SQLitePersistence(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (6,)
        assert connection.execute("SELECT calendar_context_id FROM study_results").fetchone() == (
            None,
        )


def test_v5_migration_failure_rolls_back_to_v4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.db"
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 4)
    SQLitePersistence(path).initialize()
    monkeypatch.setattr(SQLitePersistence, "SCHEMA_VERSION", 5)
    original = SQLitePersistence._migrate_to_version_5

    def fail_after_schema_work(
        persistence: SQLitePersistence, connection: sqlite3.Connection
    ) -> None:
        original(persistence, connection)
        raise sqlite3.OperationalError("injected migration failure")

    monkeypatch.setattr(SQLitePersistence, "_migrate_to_version_5", fail_after_schema_work)
    with pytest.raises(MigrationError, match="rolled back"):
        SQLitePersistence(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (4,)
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "study_events" not in names
        assert "study_event_observations" not in names


def test_append_only_event_tables_reject_update_and_delete(tmp_path: Path) -> None:
    _result, _store, _bars = _run(
        tmp_path / "append-only.db",
        (5, 9, 6),
        (2, 3, 4),
    )
    with sqlite3.connect(tmp_path / "append-only.db") as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE study_events SET price = 999")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM study_events")
