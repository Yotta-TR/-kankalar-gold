"""Calendar and session-gap tests use synthetic schedules only."""

from datetime import date, datetime, time, timedelta, timezone

import pytest

from kankalar_gold_ai.data.calendar import (
    CalendarClosure,
    CalendarProvenance,
    SessionCalendar,
    SessionState,
    WeeklySession,
)

UTC = timezone.utc


def calendar(
    *,
    timezone_name: str = "UTC",
    sessions: tuple[WeeklySession, ...] = (),
    holidays: frozenset[date] = frozenset(),
    closures: tuple[CalendarClosure, ...] = (),
    start: datetime = datetime(2026, 1, 1, tzinfo=UTC),
    end: datetime = datetime(2027, 1, 1, tzinfo=UTC),
    effective_start: datetime | None = None,
    effective_end: datetime | None = None,
) -> SessionCalendar:
    return SessionCalendar(
        calendar_id="synthetic.calendar",
        version="calendar-test-v1",
        provider_id="synthetic-test-provider",
        instrument_id="synthetic-test-instrument",
        provenance=CalendarProvenance(
            source="synthetic-test-fixture",
            reference="tests/test_session_calendar.py",
        ),
        timezone_name=timezone_name,
        effective_start_utc=effective_start or start,
        effective_end_utc=effective_end or end,
        coverage_start_utc=start,
        coverage_end_utc=end,
        weekly_sessions=sessions,
        holidays=holidays,
        exceptional_closures=closures,
    )


def test_weekly_sessions_and_daily_breaks_are_classified() -> None:
    schedule = calendar(
        sessions=(
            WeeklySession(0, time(9), time(12)),
            WeeklySession(0, time(13), time(17)),
        )
    )
    day = datetime(2026, 1, 5, tzinfo=UTC)

    before_open = schedule.classify_interval(day + timedelta(hours=8), day + timedelta(hours=9))
    lunch_break = schedule.classify_interval(day + timedelta(hours=12), day + timedelta(hours=13))
    second_session = schedule.classify_interval(
        day + timedelta(hours=13), day + timedelta(hours=14)
    )

    assert before_open.state is SessionState.CLOSED
    assert lunch_break.state is SessionState.CLOSED
    assert second_session.state is SessionState.OPEN


def test_overnight_session_crosses_midnight() -> None:
    schedule = calendar(sessions=(WeeklySession(2, time(22), time(2), close_day_offset=1),))
    start = datetime(2026, 1, 7, 22, tzinfo=UTC)

    result = schedule.classify_interval(start, start + timedelta(hours=4))

    assert result.state is SessionState.OPEN
    assert result.open_duration == timedelta(hours=4)


@pytest.mark.parametrize("offset", [True, False, 0.0, 1.0, -1, 2])
def test_weekly_session_rejects_non_integer_or_out_of_range_offset(offset: object) -> None:
    with pytest.raises(ValueError, match="close_day_offset"):
        WeeklySession(0, time(9), time(17), close_day_offset=offset)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("offset", "opens", "closes"),
    [(0, time(9), time(17)), (1, time(22), time(2)), (1, time(0), time(0))],
)
def test_weekly_session_accepts_integer_offsets(offset: int, opens: time, closes: time) -> None:
    assert WeeklySession(0, opens, closes, close_day_offset=offset).close_day_offset == offset


def test_full_local_day_session_is_accepted() -> None:
    session = WeeklySession(6, time(0), time(0), close_day_offset=1)
    schedule = calendar(timezone_name="UTC", sessions=(session,))
    start = datetime(2026, 1, 4, tzinfo=UTC)

    result = schedule.classify_interval(start, start + timedelta(days=1))

    assert result.state is SessionState.OPEN
    assert result.open_duration == timedelta(days=1)
    with pytest.raises(ValueError, match="same-day session close"):
        WeeklySession(6, time(0), time(0))


def test_full_local_day_session_uses_actual_dst_elapsed_duration() -> None:
    schedule = calendar(
        timezone_name="Europe/London",
        sessions=(WeeklySession(6, time(0), time(0), close_day_offset=1),),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2027, 1, 1, tzinfo=UTC),
    )
    spring_start = datetime(2026, 3, 29, tzinfo=UTC)
    autumn_start = datetime(2026, 10, 24, 23, tzinfo=UTC)

    spring = schedule.classify_interval(spring_start, spring_start + timedelta(days=1))
    autumn = schedule.classify_interval(autumn_start, autumn_start + timedelta(hours=25))

    assert spring.open_duration == timedelta(hours=23)
    assert autumn.open_duration == timedelta(hours=25)


def test_holiday_and_exceptional_closure_override_weekly_open_session() -> None:
    exceptional_start = datetime(2026, 1, 5, 10, tzinfo=UTC)
    schedule = calendar(
        sessions=(WeeklySession(0, time(9), time(17)),),
        holidays=frozenset({date(2026, 1, 6)}),
        closures=(
            CalendarClosure(
                exceptional_start,
                exceptional_start + timedelta(hours=1),
                "synthetic exceptional closure",
            ),
        ),
    )

    holiday = schedule.classify_interval(
        datetime(2026, 1, 6, 10, tzinfo=UTC), datetime(2026, 1, 6, 11, tzinfo=UTC)
    )
    exceptional = schedule.classify_interval(
        exceptional_start, exceptional_start + timedelta(hours=1)
    )

    assert holiday.state is SessionState.CLOSED
    assert exceptional.state is SessionState.CLOSED


def test_dst_conversion_uses_iana_timezone_elapsed_time() -> None:
    schedule = calendar(
        timezone_name="Europe/London",
        sessions=(WeeklySession(6, time(0, 30), time(3, 30)),),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2027, 1, 1, tzinfo=UTC),
    )
    spring_start = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)
    autumn_start = datetime(2026, 10, 24, 23, 30, tzinfo=UTC)

    spring = schedule.classify_interval(spring_start, spring_start + timedelta(hours=2))
    autumn = schedule.classify_interval(autumn_start, autumn_start + timedelta(hours=4))

    assert spring.state is SessionState.OPEN
    assert spring.open_duration == timedelta(hours=2)
    assert autumn.state is SessionState.OPEN
    assert autumn.open_duration == timedelta(hours=4)


def test_ambiguous_dst_session_boundary_requires_explicit_fold() -> None:
    schedule = calendar(
        timezone_name="Europe/London",
        sessions=(WeeklySession(6, time(1, 30), time(2, 30)),),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2027, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="explicit fold"):
        schedule.classify_interval(
            datetime(2026, 10, 25, 0, tzinfo=UTC),
            datetime(2026, 10, 25, 3, tzinfo=UTC),
        )


def test_fall_back_fold_one_session_classifies_absolute_utc_intervals() -> None:
    schedule = calendar(
        timezone_name="Europe/London",
        sessions=(WeeklySession(6, time(1, 30), time(2, 30), open_fold=1),),
        start=datetime(2026, 10, 24, tzinfo=UTC),
        end=datetime(2026, 10, 27, tzinfo=UTC),
    )

    result = schedule.classify_interval(
        datetime(2026, 10, 25, 0, 45, tzinfo=UTC),
        datetime(2026, 10, 25, 2, 45, tzinfo=UTC),
    )

    assert result.closed_duration == timedelta(minutes=60)
    assert result.open_duration == timedelta(hours=1)
    assert result.unknown_duration == timedelta()
    assert tuple(segment.state for segment in result.segments) == (
        SessionState.CLOSED,
        SessionState.OPEN,
        SessionState.CLOSED,
    )
    assert tuple(segment.duration for segment in result.segments) == (
        timedelta(minutes=45),
        timedelta(hours=1),
        timedelta(minutes=15),
    )


@pytest.mark.parametrize(
    ("open_fold", "expected_open"),
    [
        (0, datetime(2026, 10, 25, 0, 30, tzinfo=UTC)),
        (1, datetime(2026, 10, 25, 1, 30, tzinfo=UTC)),
    ],
)
def test_fall_back_open_fold_selects_the_requested_occurrence(
    open_fold: int,
    expected_open: datetime,
) -> None:
    schedule = calendar(
        timezone_name="Europe/London",
        sessions=(WeeklySession(6, time(1, 30), time(2, 30), open_fold=open_fold),),
        start=datetime(2026, 10, 24, tzinfo=UTC),
        end=datetime(2026, 10, 27, tzinfo=UTC),
    )

    result = schedule.classify_interval(
        expected_open - timedelta(minutes=1),
        datetime(2026, 10, 25, 2, 31, tzinfo=UTC),
    )

    assert result.open_duration == datetime(2026, 10, 25, 2, 30, tzinfo=UTC) - expected_open


@pytest.mark.parametrize(
    ("close_fold", "expected_close"),
    [
        (0, datetime(2026, 10, 25, 0, 30, tzinfo=UTC)),
        (1, datetime(2026, 10, 25, 1, 30, tzinfo=UTC)),
    ],
)
def test_fall_back_close_fold_selects_the_requested_occurrence(
    close_fold: int,
    expected_close: datetime,
) -> None:
    schedule = calendar(
        timezone_name="Europe/London",
        sessions=(WeeklySession(6, time(0, 30), time(1, 30), close_fold=close_fold),),
        start=datetime(2026, 10, 24, tzinfo=UTC),
        end=datetime(2026, 10, 27, tzinfo=UTC),
    )

    result = schedule.classify_interval(
        datetime(2026, 10, 24, 23, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 31, tzinfo=UTC),
    )

    assert result.open_duration == expected_close - datetime(2026, 10, 24, 23, 30, tzinfo=UTC)


def test_classification_is_deterministic_and_normalizes_utc_offsets() -> None:
    schedule = calendar(sessions=(WeeklySession(0, time(9), time(17)),))
    start = datetime(2026, 1, 5, 9, tzinfo=UTC)
    offset = timezone(timedelta(hours=3))

    first = schedule.classify_interval(start, start + timedelta(hours=1))
    second = schedule.classify_interval(
        start.astimezone(offset), (start + timedelta(hours=1)).astimezone(offset)
    )

    assert first == second


def test_coverage_boundaries_are_half_open_and_unknown_outside() -> None:
    schedule = calendar(
        sessions=(WeeklySession(0, time(9), time(17)),),
        start=datetime(2026, 1, 5, 9, tzinfo=UTC),
        end=datetime(2026, 1, 5, 10, tzinfo=UTC),
    )

    inside = schedule.classify_interval(
        datetime(2026, 1, 5, 9, tzinfo=UTC), datetime(2026, 1, 5, 10, tzinfo=UTC)
    )
    outside = schedule.classify_interval(
        datetime(2026, 1, 5, 9, 30, tzinfo=UTC), datetime(2026, 1, 5, 10, 30, tzinfo=UTC)
    )

    assert inside.state is SessionState.OPEN
    assert outside.state is SessionState.UNKNOWN
    assert outside.unknown_duration == timedelta(minutes=30)
    assert outside.open_duration == timedelta(minutes=30)


@pytest.mark.parametrize(
    ("open_time", "valid"),
    [
        (datetime(2026, 1, 5, 8, 55, tzinfo=UTC), False),
        (datetime(2026, 1, 5, 9, 58, tzinfo=UTC), False),
        (datetime(2026, 1, 5, 10, 0, tzinfo=UTC), False),
        (datetime(2026, 1, 5, 9, 0, tzinfo=UTC), True),
        (datetime(2026, 1, 5, 9, 55, tzinfo=UTC), True),
    ],
)
def test_single_bar_requires_full_calendar_coverage(open_time: datetime, valid: bool) -> None:
    from kankalar_gold_ai.core.contracts import DataStatus, MarketBar
    from kankalar_gold_ai.data.errors import HistoricalDataQualityError
    from kankalar_gold_ai.data.quality import validate_bars
    from kankalar_gold_ai.data.validated import ValidatedBarSet

    schedule = calendar(
        start=datetime(2026, 1, 5, 9, tzinfo=UTC),
        end=datetime(2026, 1, 5, 10, tzinfo=UTC),
        effective_start=datetime(2026, 1, 5, 8, tzinfo=UTC),
        effective_end=datetime(2026, 1, 5, 11, tzinfo=UTC),
    )
    bar = MarketBar(
        "synthetic-test-instrument",
        "M5",
        open_time,
        100,
        102,
        99,
        101,
        None,
        "synthetic-test-provider",
    )
    now = datetime(2026, 1, 5, 11, tzinfo=UTC)
    report = validate_bars((bar,), "M5", now, calendar=schedule)
    if valid:
        assert report.status is DataStatus.VALID
        ValidatedBarSet.from_bars((bar,), "M5", now, cutoff_utc=now, calendar=schedule)
    else:
        assert report.status is DataStatus.INSUFFICIENT_DATA
        assert "CALENDAR_COVERAGE_UNKNOWN" in report.reasons
        with pytest.raises(HistoricalDataQualityError, match="CALENDAR_COVERAGE_UNKNOWN"):
            ValidatedBarSet.from_bars((bar,), "M5", now, cutoff_utc=now, calendar=schedule)


def test_outside_coverage_does_not_resolve_nonexistent_dst_session_boundary() -> None:
    schedule = calendar(
        timezone_name="Europe/London",
        sessions=(WeeklySession(6, time(1, 30), time(3, 30)),),
        start=datetime(2026, 3, 29, 1, tzinfo=UTC),
        end=datetime(2026, 4, 1, tzinfo=UTC),
        effective_start=datetime(2026, 3, 29, 1, tzinfo=UTC),
        effective_end=datetime(2026, 4, 1, tzinfo=UTC),
    )

    result = schedule.classify_interval(
        datetime(2026, 3, 29, 0, 30, tzinfo=UTC),
        datetime(2026, 3, 29, 1, tzinfo=UTC),
    )

    assert result.state is SessionState.UNKNOWN
    assert result.unknown_duration == timedelta(minutes=30)


@pytest.mark.parametrize(
    ("start_utc", "day_length"),
    [
        (datetime(2026, 3, 29, 0, tzinfo=UTC), timedelta(hours=23)),
        (datetime(2026, 10, 24, 23, tzinfo=UTC), timedelta(hours=25)),
    ],
)
def test_calendar_scoped_d1_local_midnights_are_consecutive(
    start_utc: datetime,
    day_length: timedelta,
) -> None:
    from kankalar_gold_ai.core.contracts import MarketBar
    from kankalar_gold_ai.data.quality import validate_bars

    schedule = calendar(
        timezone_name="Europe/London",
        sessions=tuple(
            WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(7)
        ),
        start=start_utc - timedelta(days=1),
        end=start_utc + day_length + timedelta(days=1),
    )
    second_open = start_utc + day_length
    bars = (
        MarketBar(
            "synthetic-test-instrument",
            "D1",
            start_utc,
            100,
            102,
            99,
            101,
            None,
            "synthetic-test-provider",
        ),
        MarketBar(
            "synthetic-test-instrument",
            "D1",
            second_open,
            101,
            103,
            100,
            102,
            None,
            "synthetic-test-provider",
        ),
    )

    report = validate_bars(
        bars,
        "D1",
        schedule.next_local_day_boundary(second_open) + timedelta(minutes=1),
        calendar=schedule,
    )

    assert report.status.value == "VALID"
    assert "MISSING_BAR" not in report.reasons


def test_calendar_scoped_25_hour_d1_bar_is_incomplete_after_24_hours() -> None:
    from kankalar_gold_ai.core.contracts import MarketBar
    from kankalar_gold_ai.data.quality import validate_bars

    schedule = calendar(
        timezone_name="Europe/London",
        sessions=tuple(
            WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(7)
        ),
        start=datetime(2026, 10, 24, 23, tzinfo=UTC),
        end=datetime(2026, 10, 28, tzinfo=UTC),
    )
    bar = MarketBar(
        "synthetic-test-instrument",
        "D1",
        datetime(2026, 10, 24, 23, tzinfo=UTC),
        100,
        102,
        99,
        101,
        None,
        "synthetic-test-provider",
    )

    report = validate_bars(
        (bar,),
        "D1",
        bar.timestamp_utc + timedelta(hours=24),
        calendar=schedule,
    )

    assert report.status.value == "INSUFFICIENT_DATA"
    assert "INCOMPLETE_BAR" in report.reasons


def test_pipeline_retrieval_uses_local_completion_for_23_hour_d1_day() -> None:
    from kankalar_gold_ai.core.contracts import MarketBar
    from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline

    start = datetime(2026, 3, 29, 0, tzinfo=UTC)
    completion = datetime(2026, 3, 29, 23, tzinfo=UTC)
    schedule = calendar(
        timezone_name="Europe/London",
        sessions=tuple(
            WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(7)
        ),
        start=start - timedelta(days=1),
        end=completion + timedelta(days=1),
    )
    bar = MarketBar(
        "synthetic-test-instrument",
        "D1",
        start,
        100,
        102,
        99,
        101,
        None,
        "synthetic-test-provider",
    )

    class StaticProvider:
        def __init__(self) -> None:
            self.end_requested: datetime | None = None

        def get_bars(
            self,
            instrument: str,
            timeframe: str,
            start_utc: datetime,
            end_utc: datetime,
        ) -> tuple[MarketBar, ...]:
            self.end_requested = end_utc
            return (bar,)

    provider = StaticProvider()
    result = HistoricalDataPipeline(provider, clock=lambda: completion).retrieve_validated_bars(
        "synthetic-test-instrument",
        "D1",
        completion,
        history_days=2,
        calendar=schedule,
    )

    assert provider.end_requested == completion
    assert result.bars == (bar,)


def test_calendar_d1_retrieval_excludes_bar_opening_at_completion_cutoff() -> None:
    from kankalar_gold_ai.core.contracts import MarketBar
    from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline

    completed_open = datetime(2026, 10, 24, 23, tzinfo=UTC)
    completion = datetime(2026, 10, 26, 0, tzinfo=UTC)
    boundary_bar = MarketBar(
        "synthetic-test-instrument",
        "D1",
        completion,
        101,
        103,
        100,
        102,
        None,
        "synthetic-test-provider",
    )
    completed_bar = MarketBar(
        "synthetic-test-instrument",
        "D1",
        completed_open,
        100,
        102,
        99,
        101,
        None,
        "synthetic-test-provider",
    )
    schedule = calendar(
        timezone_name="Europe/London",
        sessions=tuple(
            WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(7)
        ),
        start=completed_open - timedelta(days=1),
        end=completion + timedelta(days=2),
    )

    class InclusiveProvider:
        def get_bars(
            self,
            instrument: str,
            timeframe: str,
            start_utc: datetime,
            end_utc: datetime,
        ) -> tuple[MarketBar, ...]:
            return (completed_bar, boundary_bar)

    result = HistoricalDataPipeline(
        InclusiveProvider(), clock=lambda: completion
    ).retrieve_validated_bars(
        "synthetic-test-instrument",
        "D1",
        completion,
        history_days=2,
        calendar=schedule,
    )

    assert result.bars == (completed_bar,)


def test_latest_complete_d1_ignores_provider_boundary_bar() -> None:
    from kankalar_gold_ai.core.contracts import MarketBar
    from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline

    completed_open = datetime(2026, 10, 24, 23, tzinfo=UTC)
    bar_end = datetime(2026, 10, 26, 0, tzinfo=UTC)
    now = datetime(2026, 10, 26, 1, tzinfo=UTC)
    completed_bar = MarketBar(
        "synthetic-test-instrument",
        "D1",
        completed_open,
        100,
        102,
        99,
        101,
        None,
        "synthetic-test-provider",
    )
    boundary_bar = MarketBar(
        "synthetic-test-instrument",
        "D1",
        bar_end,
        101,
        103,
        100,
        102,
        None,
        "synthetic-test-provider",
    )
    schedule = calendar(
        timezone_name="Europe/London",
        sessions=tuple(
            WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(7)
        ),
        start=completed_open - timedelta(days=1),
        end=now + timedelta(days=1),
    )

    class InclusiveProvider:
        def get_bars(
            self,
            instrument: str,
            timeframe: str,
            start_utc: datetime,
            end_utc: datetime,
        ) -> tuple[MarketBar, ...]:
            return (completed_bar, boundary_bar)

    result = HistoricalDataPipeline(InclusiveProvider(), clock=lambda: now).latest_complete_bar(
        "synthetic-test-instrument", "D1", calendar=schedule
    )

    assert result == completed_bar


def test_latest_complete_d1_searches_weekend_and_excludes_boundary() -> None:
    from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
    from kankalar_gold_ai.core.contracts import MarketBar
    from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline

    friday = datetime(2026, 1, 9, tzinfo=UTC)
    sunday = datetime(2026, 1, 11, tzinfo=UTC)
    now = sunday + timedelta(hours=12)
    bars = tuple(
        MarketBar(
            "synthetic-test-instrument",
            "D1",
            timestamp,
            100,
            102,
            99,
            101,
            None,
            "synthetic-test-provider",
        )
        for timestamp in (friday, sunday)
    )
    schedule = calendar(
        sessions=tuple(WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(5))
    )
    pipeline = HistoricalDataPipeline(MockMarketDataProvider(bars), clock=lambda: now)

    assert (
        pipeline.latest_complete_bar(
            "synthetic-test-instrument", "D1", calendar=schedule, lookback_days=4
        )
        == bars[0]
    )
    assert (
        pipeline.latest_complete_bar(
            "synthetic-test-instrument", "D1", calendar=schedule, lookback_days=1
        )
        is None
    )


@pytest.mark.parametrize("response", ["empty", "error", "boundary"])
@pytest.mark.parametrize("condition", ["open", "unknown", "closed", "mixed"])
def test_latest_complete_d1_no_candidates_classifies_entire_search(
    response: str, condition: str
) -> None:
    from kankalar_gold_ai.core.contracts import MarketBar
    from kankalar_gold_ai.data.errors import EmptyHistoricalDataError, HistoricalDataQualityError
    from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline

    now = datetime(2026, 1, 7, 12, tzinfo=UTC)
    cutoff = datetime(2026, 1, 7, tzinfo=UTC)
    start = now - timedelta(days=2)
    sessions = (
        tuple(WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(7))
        if condition in ("open", "mixed")
        else ()
    )
    coverage_start = start + timedelta(hours=1) if condition in ("unknown", "mixed") else start
    schedule = calendar(
        sessions=sessions,
        start=coverage_start,
        end=now + timedelta(days=1),
        effective_start=start - timedelta(days=1),
    )
    boundary = MarketBar(
        "synthetic-test-instrument",
        "D1",
        cutoff,
        100,
        102,
        99,
        101,
        None,
        "synthetic-test-provider",
    )

    class Provider:
        def get_bars(
            self, instrument: str, timeframe: str, start_utc: datetime, end_utc: datetime
        ) -> tuple[MarketBar, ...]:
            assert (start_utc, end_utc) == (start, cutoff)
            if response == "error":
                raise EmptyHistoricalDataError("synthetic empty history")
            return (boundary,) if response == "boundary" else ()

    pipeline = HistoricalDataPipeline(Provider(), clock=lambda: now)
    if condition == "closed":
        assert (
            pipeline.latest_complete_bar(
                "synthetic-test-instrument", "D1", calendar=schedule, lookback_days=2
            )
            is None
        )
    else:
        with pytest.raises(HistoricalDataQualityError) as error:
            pipeline.latest_complete_bar(
                "synthetic-test-instrument", "D1", calendar=schedule, lookback_days=2
            )
        assert set(error.value.report.reasons) == {
            *({"MISSING_BAR"} if condition in ("open", "mixed") else set()),
            *({"CALENDAR_COVERAGE_UNKNOWN"} if condition in ("unknown", "mixed") else set()),
        }


def test_latest_complete_d1_searches_past_synthetic_holiday() -> None:
    from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
    from kankalar_gold_ai.core.contracts import MarketBar
    from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline

    thursday = datetime(2026, 1, 8, tzinfo=UTC)
    saturday = datetime(2026, 1, 10, 12, tzinfo=UTC)
    bar = MarketBar(
        "synthetic-test-instrument",
        "D1",
        thursday,
        100,
        102,
        99,
        101,
        None,
        "synthetic-test-provider",
    )
    schedule = calendar(
        sessions=tuple(
            WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(5)
        ),
        holidays=frozenset({date(2026, 1, 9)}),
    )
    pipeline = HistoricalDataPipeline(MockMarketDataProvider((bar,)), clock=lambda: saturday)

    assert (
        pipeline.latest_complete_bar(
            "synthetic-test-instrument", "D1", calendar=schedule, lookback_days=4
        )
        == bar
    )


def test_latest_complete_d1_validates_scope_and_lookback() -> None:
    from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
    from kankalar_gold_ai.core.contracts import MarketBar
    from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline

    now = datetime(2026, 1, 11, 12, tzinfo=UTC)
    bar = MarketBar(
        "synthetic-test-instrument",
        "D1",
        datetime(2026, 1, 9, tzinfo=UTC),
        100,
        102,
        99,
        101,
        None,
        "different-synthetic-provider",
    )
    pipeline = HistoricalDataPipeline(MockMarketDataProvider((bar,)), clock=lambda: now)
    schedule = calendar()

    with pytest.raises(ValueError, match="instrument"):
        pipeline.latest_complete_bar("other-synthetic-instrument", "D1", calendar=schedule)
    with pytest.raises(ValueError, match="provider/source"):
        pipeline.latest_complete_bar("synthetic-test-instrument", "D1", calendar=schedule)
    for bound in (0, 367, True):
        with pytest.raises(ValueError, match="lookback_days"):
            pipeline.latest_complete_bar(
                "synthetic-test-instrument", "D1", calendar=schedule, lookback_days=bound
            )


def test_latest_complete_d1_rejects_missing_open_days() -> None:
    from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
    from kankalar_gold_ai.core.contracts import MarketBar
    from kankalar_gold_ai.data.errors import HistoricalDataQualityError
    from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline

    bars = tuple(
        MarketBar(
            "synthetic-test-instrument",
            "D1",
            datetime(2026, 1, day, tzinfo=UTC),
            100,
            102,
            99,
            101,
            None,
            "synthetic-test-provider",
        )
        for day in (5, 7)
    )
    schedule = calendar(
        sessions=tuple(WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(5))
    )
    pipeline = HistoricalDataPipeline(
        MockMarketDataProvider(bars), clock=lambda: datetime(2026, 1, 8, 12, tzinfo=UTC)
    )

    with pytest.raises(HistoricalDataQualityError, match="MISSING_BAR"):
        pipeline.latest_complete_bar(
            "synthetic-test-instrument", "D1", calendar=schedule, lookback_days=4
        )


@pytest.mark.parametrize(
    ("bar_day", "coverage_end", "reason"),
    (
        (8, datetime(2026, 1, 13, tzinfo=UTC), "MISSING_BAR"),
        (9, datetime(2026, 1, 11, tzinfo=UTC), "CALENDAR_COVERAGE_UNKNOWN"),
    ),
)
def test_latest_complete_d1_rejects_unproven_trailing_gap(
    bar_day: int, coverage_end: datetime, reason: str
) -> None:
    from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
    from kankalar_gold_ai.core.contracts import MarketBar
    from kankalar_gold_ai.data.errors import HistoricalDataQualityError
    from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline

    bar = MarketBar(
        "synthetic-test-instrument",
        "D1",
        datetime(2026, 1, bar_day, tzinfo=UTC),
        100,
        102,
        99,
        101,
        None,
        "synthetic-test-provider",
    )
    schedule = calendar(
        sessions=tuple(
            WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(5)
        ),
        end=coverage_end,
        effective_end=datetime(2026, 1, 13, tzinfo=UTC),
    )
    pipeline = HistoricalDataPipeline(
        MockMarketDataProvider((bar,)), clock=lambda: datetime(2026, 1, 12, 12, tzinfo=UTC)
    )

    with pytest.raises(HistoricalDataQualityError, match=reason):
        pipeline.latest_complete_bar(
            "synthetic-test-instrument", "D1", calendar=schedule, lookback_days=6
        )


def test_latest_complete_d1_does_not_return_unfinished_25_hour_bar() -> None:
    from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
    from kankalar_gold_ai.core.contracts import MarketBar
    from kankalar_gold_ai.data.errors import HistoricalDataQualityError
    from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline

    bar_open = datetime(2026, 10, 24, 23, tzinfo=UTC)
    bar = MarketBar(
        "synthetic-test-instrument",
        "D1",
        bar_open,
        100,
        102,
        99,
        101,
        None,
        "synthetic-test-provider",
    )
    schedule = calendar(
        timezone_name="Europe/London",
        sessions=tuple(
            WeeklySession(day, time(0), time(0), close_day_offset=1) for day in range(7)
        ),
    )
    pipeline = HistoricalDataPipeline(
        MockMarketDataProvider((bar,)), clock=lambda: bar_open + timedelta(hours=24)
    )

    with pytest.raises(HistoricalDataQualityError, match="MISSING_BAR"):
        pipeline.latest_complete_bar(
            "synthetic-test-instrument", "D1", calendar=schedule, lookback_days=3
        )


def test_latest_complete_d1_rejects_lookback_not_reaching_previous_day() -> None:
    from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
    from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline

    schedule = calendar(timezone_name="Europe/London")
    now = datetime(2026, 10, 25, 23, 30, tzinfo=UTC)
    pipeline = HistoricalDataPipeline(MockMarketDataProvider(()), clock=lambda: now)

    with pytest.raises(ValueError, match="does not reach a completed local calendar day"):
        pipeline.latest_complete_bar(
            "synthetic-test-instrument", "D1", calendar=schedule, lookback_days=1
        )


def test_invalid_calendar_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="IANA timezone"):
        calendar(timezone_name="Not/A_Real_Zone")
    with pytest.raises(ValueError, match="weekday"):
        WeeklySession(7, time(9), time(17))
    with pytest.raises(ValueError, match="must precede"):
        CalendarClosure(
            datetime(2026, 1, 5, 10, tzinfo=UTC),
            datetime(2026, 1, 5, 10, tzinfo=UTC),
            "invalid",
        )
    with pytest.raises(ValueError, match="effective start"):
        calendar(
            start=datetime(2026, 1, 6, tzinfo=UTC),
            end=datetime(2026, 1, 5, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="coverage start"):
        calendar(
            start=datetime(2026, 1, 6, tzinfo=UTC),
            end=datetime(2026, 1, 5, tzinfo=UTC),
            effective_start=datetime(2026, 1, 1, tzinfo=UTC),
            effective_end=datetime(2026, 1, 7, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="effective start"):
        calendar(
            effective_start=datetime(2026, 1, 6, tzinfo=UTC),
            effective_end=datetime(2026, 1, 5, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="within its effective interval"):
        calendar(
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 2, 1, tzinfo=UTC),
            effective_start=datetime(2026, 1, 2, tzinfo=UTC),
        )


def test_nonexistent_dst_session_boundary_fails_closed() -> None:
    schedule = calendar(
        timezone_name="Europe/London",
        sessions=(WeeklySession(6, time(1, 30), time(3, 30)),),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2027, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="nonexistent local time"):
        schedule.classify_interval(
            datetime(2026, 3, 29, 0, tzinfo=UTC),
            datetime(2026, 3, 29, 3, tzinfo=UTC),
        )


def test_timezone_scope_and_provenance_are_part_of_canonical_identity() -> None:
    utc_calendar = calendar(timezone_name="UTC")
    london_calendar = calendar(timezone_name="Europe/London")

    utc_identity = utc_calendar.canonical_identity()
    london_identity = london_calendar.canonical_identity()

    assert utc_identity["configuration_sha256"] != london_identity["configuration_sha256"]
    assert utc_identity["timezone"] == "UTC"
    assert utc_identity["provider_id"] == "synthetic-test-provider"
    assert utc_identity["instrument_id"] == "synthetic-test-instrument"
    assert utc_identity["provenance_source"] == "synthetic-test-fixture"
    assert utc_identity["provenance_reference"] == "tests/test_session_calendar.py"
    assert utc_identity["effective_start_utc"] == utc_identity["coverage_start_utc"]
    assert utc_calendar.canonical_identity_json() == calendar().canonical_identity_json()
