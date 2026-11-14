"""Point-in-time harness for the F4 no-lookahead invariant (test-only, synthetic data).

``select_completed_bars`` is an independent TEST ORACLE for what a caller is supposed to
supply. It is not a production safeguard.

Exact ``StudyResult`` equality is used everywhere; no numeric tolerance is supported.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from kankalar_gold_ai.core.contracts import MarketBar, StudyResult, as_utc
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.studies import StudyRegistry, StudyRunner, StudyRunRequest

SOURCE = "synthetic-f4-fixture"
INSTRUMENT = "TEST_GOLD"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Deliberately independent of data.quality.TIMEFRAME_DURATIONS; a test pins equality.
DURATIONS = {
    "M1": timedelta(minutes=1),
    "M5": timedelta(minutes=5),
    "M15": timedelta(minutes=15),
    "H1": timedelta(hours=1),
    "H4": timedelta(hours=4),
    "D1": timedelta(days=1),
}

Universe = tuple[MarketBar, ...]
StudyFactory = Callable[[Universe], Any]


class ScriptedClock:
    """Returns scripted values in order; raises when exhausted. Never reads real time."""

    def __init__(self, values: Sequence[Any]) -> None:
        self._values = list(values)
        self.calls = 0

    def __call__(self) -> Any:
        if self.calls >= len(self._values):
            raise AssertionError("scripted source exhausted")
        value = self._values[self.calls]
        self.calls += 1
        return value


class NullStore:
    """Never hits the cache, so a cached result can never mask a leak."""

    def __init__(self) -> None:
        self.saved: list[StudyResult] = []

    def save_study_result(self, result: StudyResult) -> None:
        self.saved.append(result)

    def find_cached_study_result(self, key: object) -> StudyResult | None:
        return None


def blind(factory: Callable[[], Any]) -> StudyFactory:
    """Adapt a zero-argument factory: ordinary studies never receive the universe."""
    return lambda _universe: factory()


def build_bar(index: int, timeframe: str, close: float, volume: float | None = None) -> MarketBar:
    return MarketBar(
        instrument=INSTRUMENT,
        timeframe=timeframe,
        timestamp_utc=START + DURATIONS[timeframe] * index,
        open=close - 0.25,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=volume,
        source=SOURCE,
    )


def build_universe(count: int, timeframe: str = "M5") -> Universe:
    """Continuous, deterministic synthetic bars (no sessions or gaps)."""
    return tuple(build_bar(i, timeframe, 100.0 + (i * 7) % 11) for i in range(count))


def bar_end(bar: MarketBar, timeframe: str) -> datetime:
    return as_utc(bar.timestamp_utc) + DURATIONS[timeframe]


def end_of(index: int, timeframe: str = "M5") -> datetime:
    return START + DURATIONS[timeframe] * (index + 1)


def select_completed_bars(universe: Universe, timeframe: str, as_of: datetime) -> Universe:
    """Oracle: bars whose end (open + duration) is <= the cutoff. Naive cutoffs are rejected."""
    cutoff = as_utc(as_of)
    return tuple(bar for bar in universe if bar_end(bar, timeframe) <= cutoff)


# -- mutators ------------------------------------------------------------------
def append_future(universe: Universe, timeframe: str, count: int = 5) -> Universe:
    last = len(universe)
    extra = tuple(build_bar(last + k, timeframe, 500.0 + k, volume=7.0) for k in range(count))
    return universe + extra


def alter_future(universe: Universe, timeframe: str, cutoff: datetime) -> Universe:
    """Replace OHLCV (including volume None -> value) of bars ending after the cutoff."""
    limit = as_utc(cutoff)
    out: list[MarketBar] = []
    for i, bar in enumerate(universe):
        if bar_end(bar, timeframe) > limit:
            poison = 9000.0 + i
            bar = replace(
                bar,
                open=poison,
                high=poison + 1.0,
                low=poison - 1.0,
                close=poison,
                volume=12345.0 + i,
            )
        out.append(bar)
    return tuple(out)


def alter_close(universe: Universe, index: int, delta: float) -> Universe:
    bar = universe[index]
    close = bar.close + delta
    changed = replace(bar, close=close, high=max(bar.high, close), low=min(bar.low, close))
    return universe[:index] + (changed,) + universe[index + 1 :]


def bump_last_selected_close(
    timeframe: str,
) -> Callable[[Universe, datetime], Universe]:
    """Sensitive in-window mutation for a last-three-closes canary."""

    def mutate(universe: Universe, cutoff: datetime) -> Universe:
        selected = select_completed_bars(universe, timeframe, cutoff)
        return alter_close(universe, len(selected) - 1, 5.0)

    return mutate


# -- running -------------------------------------------------------------------
def make_request(
    study: Any,
    timeframe: str,
    cutoff: datetime,
    config: dict[str, Any] | None = None,
) -> StudyRunRequest:
    return StudyRunRequest(
        study_id=study.study_id,
        version=study.version,
        instrument=INSTRUMENT,
        timeframe=timeframe,
        as_of_utc=cutoff,
        config=config or {},
    )


def run_at(
    study: Any,
    universe: Universe,
    timeframe: str,
    cutoff: datetime,
    *,
    config: dict[str, Any] | None = None,
    validated_at: datetime | None = None,
    store: Any = None,
) -> StudyResult:
    selected = select_completed_bars(universe, timeframe, cutoff)
    bars = ValidatedBarSet.from_bars(selected, timeframe, validated_at or cutoff, cutoff_utc=cutoff)
    registry = StudyRegistry()
    registry.register(study)
    runner = StudyRunner(registry, store if store is not None else NullStore())
    return runner.run(make_request(study, timeframe, cutoff, config), bars)


OUTPUT_FIELDS = (
    "status",
    "state",
    "value",
    "normalized_value",
    "direction",
    "uncertainty",
    "evidence",
    "limitations",
)


def output_fields(result: StudyResult) -> tuple[Any, ...]:
    return tuple(getattr(result, name) for name in OUTPUT_FIELDS)


def assert_same_cutoff_equal(first: StudyResult, second: StudyResult, label: str) -> None:
    """C1/C2: selection identity first, then the full result under exact equality."""
    if first.data_snapshot_id != second.data_snapshot_id or first.provenance != second.provenance:
        raise AssertionError(f"SELECTION_CHANGED: {label}: snapshot or provenance differs")
    if first != second:
        raise AssertionError(f"FUTURE_LEAK: {label}: result differs for an identical snapshot")


def assert_cutoff_moved_within_bar(first: StudyResult, second: StudyResult) -> None:
    """C3: only as_of_utc may differ."""
    assert first.as_of_utc != second.as_of_utc
    assert replace(second, as_of_utc=first.as_of_utc) == first


@dataclass(frozen=True)
class NoLookaheadReport:
    cutoffs_checked: int
    distinct_snapshots: int
    window_changed_pairs: int


def assert_no_lookahead(
    study_factory: StudyFactory,
    universe: Universe,
    timeframe: str,
    cutoffs: Sequence[datetime],
    *,
    config: dict[str, Any] | None = None,
    sensitive_mutation: Callable[[Universe, datetime], Universe] | None = None,
) -> NoLookaheadReport:
    """Run the four-way no-lookahead checks; failures are labelled by cause.

    SELECTION_CHANGED  selection/snapshot/provenance moved under a future-only mutation
    FUTURE_LEAK        same snapshot, different result under a future-only mutation
    NOT_REPEATABLE     same cutoff and snapshot, different result across fresh runs
    REPAINT            result depends on evaluation order with a reused study instance
    VACUOUS_MUTATION   a supplied mutation did not change what it claims to change
    INSENSITIVE        a declared sensitive mutation changed the snapshot but not the output
    VACUOUS_CUTOFFS    the cutoffs never produced different snapshots
    """
    baselines: dict[datetime, StudyResult] = {}
    for cutoff in cutoffs:
        base = run_at(study_factory(universe), universe, timeframe, cutoff, config=config)
        again = run_at(study_factory(universe), universe, timeframe, cutoff, config=config)
        if base != again:
            raise AssertionError(f"NOT_REPEATABLE: {cutoff}")
        baselines[cutoff] = base

        appended = append_future(universe, timeframe)
        altered = alter_future(universe, timeframe, cutoff)
        if len(appended) <= len(universe) or altered == universe:
            raise AssertionError("VACUOUS_MUTATION: future mutation changed nothing")
        for label, variant in (("append", appended), ("alter", altered)):
            if select_completed_bars(variant, timeframe, cutoff) != select_completed_bars(
                universe, timeframe, cutoff
            ):
                raise AssertionError(f"SELECTION_CHANGED: {label} at {cutoff}")
            result = run_at(study_factory(variant), variant, timeframe, cutoff, config=config)
            assert_same_cutoff_equal(base, result, f"{label} at {cutoff}")

        if sensitive_mutation is not None:
            mutated = sensitive_mutation(universe, cutoff)
            changed = run_at(study_factory(mutated), mutated, timeframe, cutoff, config=config)
            if changed.data_snapshot_id == base.data_snapshot_id:
                raise AssertionError("VACUOUS_MUTATION: sensitive mutation left the snapshot")
            if output_fields(changed) == output_fields(base):
                raise AssertionError("INSENSITIVE: sensitive mutation did not move the output")

    shared = study_factory(universe)
    for order in (list(cutoffs), list(reversed(cutoffs))):
        for cutoff in order:
            result = run_at(shared, universe, timeframe, cutoff, config=config)
            if result != baselines[cutoff]:
                raise AssertionError(f"REPAINT: {cutoff}")

    snapshots = [baselines[c].data_snapshot_id for c in cutoffs]
    distinct = len(set(snapshots))
    if distinct < 2:
        raise AssertionError("VACUOUS_CUTOFFS: cutoffs never changed the snapshot")
    changed_pairs = sum(1 for a, b in zip(snapshots, snapshots[1:]) if a != b)
    return NoLookaheadReport(len(cutoffs), distinct, changed_pairs)
