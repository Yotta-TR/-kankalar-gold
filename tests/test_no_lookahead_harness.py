"""F4 no-lookahead harness tests: honest canary, negative controls, boundaries, determinism.

Exact StudyResult equality only (numeric tolerance is deferred). Synthetic data, scripted
environment sources, temporary SQLite only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kankalar_gold_ai.core.contracts import StudyResult
from kankalar_gold_ai.core.study_results import StudyResultKey
from kankalar_gold_ai.data.quality import TIMEFRAME_DURATIONS
from kankalar_gold_ai.storage.sqlite import SQLitePersistence
from kankalar_gold_ai.studies import StudyRequestError, StudyRunRequest
from tests.fixtures.faulty_studies import (
    CausalMean3,
    ClockStudy,
    ConstantStudy,
    RandomStudy,
    RepaintingStudy,
    make_future_peeker,
)
from tests.fixtures.point_in_time import (
    DURATIONS,
    START,
    NullStore,
    ScriptedClock,
    alter_close,
    alter_future,
    append_future,
    assert_cutoff_moved_within_bar,
    assert_no_lookahead,
    blind,
    build_bar,
    build_universe,
    bump_last_selected_close,
    end_of,
    output_fields,
    run_at,
    select_completed_bars,
)

UNIVERSE = build_universe(30)
CUTOFFS = [end_of(9), end_of(14), end_of(19)]


def _canary() -> CausalMean3:
    return CausalMean3()


class CountingStore:
    def __init__(self, inner: SQLitePersistence) -> None:
        self.inner = inner
        self.saves = 0

    def save_study_result(self, result: StudyResult) -> None:
        self.saves += 1
        self.inner.save_study_result(result)

    def find_cached_study_result(self, key: StudyResultKey) -> StudyResult | None:
        return self.inner.find_cached_study_result(key)


# -- oracle (A1, A2) -----------------------------------------------------------
def test_a1_oracle_selects_exact_completed_prefix() -> None:
    cutoff = end_of(9)
    selected = select_completed_bars(UNIVERSE, "M5", cutoff)
    assert selected == UNIVERSE[:10]
    assert selected[-1].timestamp_utc + DURATIONS["M5"] <= cutoff
    assert UNIVERSE[10].timestamp_utc + DURATIONS["M5"] > cutoff


def test_a2_oracle_durations_match_production_table() -> None:
    assert DURATIONS == TIMEFRAME_DURATIONS


# -- honest canary and negative controls (A3-A11) ------------------------------
def test_a3_a4_a6_honest_canary_passes_all_checks() -> None:
    report = assert_no_lookahead(
        blind(_canary),
        UNIVERSE,
        "M5",
        CUTOFFS,
        sensitive_mutation=bump_last_selected_close("M5"),
    )
    assert report.cutoffs_checked == 3
    assert report.distinct_snapshots == 3
    assert report.window_changed_pairs == 2


def test_a5_future_peeking_study_is_detected() -> None:
    with pytest.raises(AssertionError, match="FUTURE_LEAK"):
        assert_no_lookahead(make_future_peeker, UNIVERSE, "M5", CUTOFFS)


def test_a5_future_peeker_really_reads_the_future() -> None:
    cutoff = end_of(9)
    altered = alter_future(UNIVERSE, "M5", cutoff)
    base = run_at(make_future_peeker(UNIVERSE), UNIVERSE, "M5", cutoff)
    leaked = run_at(make_future_peeker(altered), altered, "M5", cutoff)
    assert base.data_snapshot_id == leaked.data_snapshot_id
    assert base.provenance == leaked.provenance
    assert base.value != leaked.value


def test_a7_earlier_window_change_moves_snapshot_but_not_canary_output() -> None:
    cutoff = end_of(9)
    base = run_at(_canary(), UNIVERSE, "M5", cutoff)
    earlier = alter_close(UNIVERSE, 2, 5.0)
    changed = run_at(_canary(), earlier, "M5", cutoff)
    assert changed.data_snapshot_id != base.data_snapshot_id
    assert output_fields(changed) == output_fields(base)


def test_a8_harness_cannot_pass_vacuously() -> None:
    # A real study may legitimately return a constant: no sensitivity claim, no failure.
    assert_no_lookahead(blind(ConstantStudy), UNIVERSE, "M5", CUTOFFS)
    # A declared sensitive mutation that cannot move the output is a test-authoring error.
    with pytest.raises(AssertionError, match="INSENSITIVE"):
        assert_no_lookahead(
            blind(ConstantStudy),
            UNIVERSE,
            "M5",
            CUTOFFS,
            sensitive_mutation=bump_last_selected_close("M5"),
        )
    # A "sensitive" mutation that changes nothing is vacuous.
    with pytest.raises(AssertionError, match="VACUOUS_MUTATION"):
        assert_no_lookahead(
            blind(_canary), UNIVERSE, "M5", CUTOFFS, sensitive_mutation=lambda u, c: u
        )
    # A cutoff with no future data cannot exercise a future mutation.
    with pytest.raises(AssertionError, match="VACUOUS_MUTATION"):
        assert_no_lookahead(blind(_canary), UNIVERSE, "M5", [end_of(29)])
    # Cutoffs that never change the snapshot prove nothing about forward movement.
    with pytest.raises(AssertionError, match="VACUOUS_CUTOFFS"):
        assert_no_lookahead(
            blind(_canary), UNIVERSE, "M5", [end_of(9), end_of(9) + timedelta(minutes=1)]
        )


def test_a9_repainting_study_is_detected_by_evaluation_order() -> None:
    with pytest.raises(AssertionError, match="REPAINT"):
        assert_no_lookahead(blind(RepaintingStudy), UNIVERSE, "M5", CUTOFFS)


def test_a10_scripted_clock_and_random_nondeterminism_is_detected() -> None:
    clock = ScriptedClock([float(i) for i in range(1000)])
    with pytest.raises(AssertionError, match="NOT_REPEATABLE"):
        assert_no_lookahead(blind(lambda: ClockStudy(clock)), UNIVERSE, "M5", CUTOFFS)
    draws = ScriptedClock([0.01 * i for i in range(1000)])
    with pytest.raises(AssertionError, match="NOT_REPEATABLE"):
        assert_no_lookahead(blind(lambda: RandomStudy(draws)), UNIVERSE, "M5", CUTOFFS)


def test_a11_repeated_runs_are_identical_including_identity() -> None:
    first = run_at(_canary(), UNIVERSE, "M5", end_of(9))
    second = run_at(_canary(), build_universe(30), "M5", end_of(9))
    assert first == second
    assert first.data_snapshot_id == second.data_snapshot_id
    assert first.provenance == second.provenance


def test_a12_result_does_not_depend_on_validation_time() -> None:
    cutoff = end_of(9)
    base = run_at(_canary(), UNIVERSE, "M5", cutoff)
    later = run_at(_canary(), UNIVERSE, "M5", cutoff, validated_at=cutoff + timedelta(hours=7))
    assert later == base


# -- cutoff movement (A13, A14) ------------------------------------------------
def test_a13_cutoff_inside_same_bar_changes_only_identity(tmp_path: Path) -> None:
    inner = SQLitePersistence(tmp_path / "a13.db")
    inner.initialize()
    store = CountingStore(inner)
    study = _canary()
    first = run_at(study, UNIVERSE, "M5", end_of(9) + timedelta(minutes=1), store=store)
    second = run_at(study, UNIVERSE, "M5", end_of(9) + timedelta(minutes=3), store=store)
    assert_cutoff_moved_within_bar(first, second)
    assert study.calls == 2 and store.saves == 2  # distinct cache identity: no sharing


def test_a14_cutoff_crossing_a_bar_end_legitimately_changes_result() -> None:
    first = run_at(_canary(), UNIVERSE, "M5", end_of(9))
    second = run_at(_canary(), UNIVERSE, "M5", end_of(10))
    assert first.data_snapshot_id != second.data_snapshot_id
    assert first.provenance != second.provenance
    assert first.value != second.value


# -- boundaries (A15, A16) -----------------------------------------------------
def test_a15_incomplete_bar_and_exact_boundary() -> None:
    cutoff = end_of(9)
    assert select_completed_bars(UNIVERSE, "M5", cutoff) == UNIVERSE[:10]
    just_before = cutoff - timedelta(microseconds=1)
    assert select_completed_bars(UNIVERSE, "M5", just_before) == UNIVERSE[:9]
    # A bar opening exactly at the cutoff is entirely future.
    assert UNIVERSE[10].timestamp_utc == cutoff
    assert UNIVERSE[10] not in select_completed_bars(UNIVERSE, "M5", cutoff)
    # Altering the still-forming bar changes nothing at the earlier cutoff.
    base = run_at(_canary(), UNIVERSE, "M5", just_before)
    forming_altered = alter_close(UNIVERSE, 9, 50.0)
    assert run_at(_canary(), forming_altered, "M5", just_before) == base
    # Including it does change the result.
    assert run_at(_canary(), forming_altered, "M5", cutoff) != run_at(
        _canary(), UNIVERSE, "M5", cutoff
    )


def test_a16_timezone_boundaries() -> None:
    cutoff = end_of(9)
    base = run_at(_canary(), UNIVERSE, "M5", cutoff)
    for offset in (timezone(timedelta(hours=3)), timezone(timedelta(hours=-5))):
        local = cutoff.astimezone(offset)
        assert select_completed_bars(UNIVERSE, "M5", local) == UNIVERSE[:10]
        result = run_at(_canary(), UNIVERSE, "M5", local)
        assert result == base
        assert result.as_of_utc.utcoffset() == timedelta(0)

    with pytest.raises(ValueError):
        select_completed_bars(UNIVERSE, "M5", datetime(2026, 1, 1, 1, 0))
    with pytest.raises(StudyRequestError):
        StudyRunRequest("a", "1", "X", "M5", datetime(2026, 1, 1, 1, 0))

    plus3 = timezone(timedelta(hours=3))
    offset_bar = build_bar(0, "M5", 100.0)
    offset_bar = type(offset_bar)(
        instrument=offset_bar.instrument,
        timeframe="M5",
        timestamp_utc=START.astimezone(plus3),
        open=offset_bar.open,
        high=offset_bar.high,
        low=offset_bar.low,
        close=offset_bar.close,
        volume=None,
        source=offset_bar.source,
    )
    assert offset_bar == build_bar(0, "M5", 100.0)


def test_a16_daily_boundaries() -> None:
    daily = build_universe(10, "D1")
    cutoff = START + timedelta(days=3)
    assert select_completed_bars(daily, "D1", cutoff) == daily[:3]
    assert select_completed_bars(daily, "D1", cutoff - timedelta(seconds=1)) == daily[:2]
    study = CausalMean3("D1")
    result = run_at(study, daily, "D1", cutoff)
    assert (
        result.provenance
        == run_at(CausalMean3("D1"), append_future(daily, "D1"), "D1", cutoff).provenance
    )
    assert dict(result.provenance)["input_window_end_utc"].startswith("2026-01-04T00:00:00")


# -- cache pitfall (A17) -------------------------------------------------------
def test_a17_shared_cache_would_mask_a_leak_so_harness_uses_fresh_stores(tmp_path: Path) -> None:
    inner = SQLitePersistence(tmp_path / "a17.db")
    inner.initialize()
    store = CountingStore(inner)
    cutoff = end_of(9)
    peeker_base = make_future_peeker(UNIVERSE)
    altered = alter_future(UNIVERSE, "M5", cutoff)
    first = run_at(peeker_base, UNIVERSE, "M5", cutoff, store=store)
    leaker = make_future_peeker(altered)
    second = run_at(leaker, altered, "M5", cutoff, store=store)
    assert second == first  # the cache hides the leak ...
    assert leaker.calls == 0 and store.saves == 1  # ... without computing or saving again
    fresh = run_at(make_future_peeker(altered), altered, "M5", cutoff, store=NullStore())
    assert fresh != first  # a fresh store exposes it
