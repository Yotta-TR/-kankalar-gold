"""F4 production-boundary characterization: real pipeline, ValidatedBarSet and StudyRunner.

Findings recorded here (see ADR-008):
* ``HistoricalDataPipeline`` passes ``end_utc`` to the provider and never inspects the
  returned bars against it; ``end_utc`` bounds bar OPEN time for the Mock and MT5 providers.
* ``ValidatedBarSet`` only checks completeness relative to ``validated_at_utc``.
* ``StudyRunner`` independently rejects bars ending after the supplied ``as_of_utc``.
* Nothing binds the pipeline ``end_utc`` to the runner ``as_of_utc``.
Synthetic data and a scripted clock only; no network, no MT5.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
from kankalar_gold_ai.core.contracts import StudyResult
from kankalar_gold_ai.core.study_results import StudyResultKey
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import SQLitePersistence
from kankalar_gold_ai.studies import StudyRegistry, StudyRequestError, StudyRunner
from tests.fixtures.adversarial_provider import OverreachingProvider
from tests.fixtures.faulty_studies import CausalMean3
from tests.fixtures.point_in_time import (
    DURATIONS,
    INSTRUMENT,
    ScriptedClock,
    alter_future,
    bar_end,
    build_universe,
    end_of,
    make_request,
    select_completed_bars,
)

UNIVERSE = build_universe(10)
CUTOFF = end_of(4)  # 00:25, the intended analysis cutoff
NOW = end_of(9)  # 00:50: every universe bar is complete at validation time


class CountingStore:
    def __init__(self, inner: SQLitePersistence) -> None:
        self.inner = inner
        self.saves = 0

    def save_study_result(self, result: StudyResult) -> None:
        self.saves += 1
        self.inner.save_study_result(result)

    def find_cached_study_result(self, key: StudyResultKey) -> StudyResult | None:
        return self.inner.find_cached_study_result(key)


def _pipeline(provider: object) -> HistoricalDataPipeline:
    return HistoricalDataPipeline(provider, clock=ScriptedClock([NOW]))  # type: ignore[arg-type]


def _runner(tmp_path: Path) -> tuple[StudyRunner, CausalMean3, CountingStore]:
    study = CausalMean3()
    registry = StudyRegistry()
    registry.register(study)
    inner = SQLitePersistence(tmp_path / "boundary.db")
    inner.initialize()
    store = CountingStore(inner)
    return StudyRunner(registry, store), study, store


def _retrieve(provider: object, end_utc: datetime) -> ValidatedBarSet:
    return _pipeline(provider).retrieve_validated_bars(INSTRUMENT, "M5", end_utc, 1)


def test_b1_compliant_provider_end_utc_bounds_open_time_so_runner_rejects(
    tmp_path: Path,
) -> None:
    runner, study, store = _runner(tmp_path)
    bars = _retrieve(MockMarketDataProvider(UNIVERSE), CUTOFF)
    # The bar opening exactly at end_utc is returned although it ends after it.
    assert len(bars.bars) == 6
    assert bar_end(bars.bars[-1], "M5") > CUTOFF
    with pytest.raises(StudyRequestError):
        runner.run(make_request(study, "M5", CUTOFF), bars)
    assert study.calls == 0 and store.saves == 0

    # A caller wanting cutoff t must pass end_utc = t - duration to a compliant provider.
    aligned = _retrieve(MockMarketDataProvider(UNIVERSE), CUTOFF - DURATIONS["M5"])
    assert aligned.bars == select_completed_bars(UNIVERSE, "M5", CUTOFF)
    assert runner.run(make_request(study, "M5", CUTOFF), aligned).as_of_utc == CUTOFF


def test_b2_pipeline_does_not_reject_or_trim_overreaching_provider(tmp_path: Path) -> None:
    provider = OverreachingProvider(UNIVERSE[:8])
    bars = _retrieve(provider, CUTOFF)
    assert provider.requests[0][3] == CUTOFF
    assert len(bars.bars) == 8  # overlong set accepted by the pipeline and ValidatedBarSet
    assert max(bar_end(b, "M5") for b in bars.bars) > CUTOFF


def test_b3_runner_rejects_overlong_set_before_compute(tmp_path: Path) -> None:
    runner, study, store = _runner(tmp_path)
    bars = _retrieve(OverreachingProvider(UNIVERSE[:8]), CUTOFF)
    with pytest.raises(StudyRequestError):
        runner.run(make_request(study, "M5", CUTOFF), bars)
    assert study.calls == 0 and study.seen_bars == [] and store.saves == 0


def test_b4_characterizes_current_gap_as_of_is_not_bound_to_pipeline_end_utc(
    tmp_path: Path,
) -> None:
    """CHARACTERIZATION of a known integration gap, NOT desired behavior.

    A caller who passes as_of_utc=NOW instead of the pipeline's end_utc gets bars ending
    after end_utc into ``compute``. That is not leakage relative to the supplied as_of_utc;
    it shows F4 depends on the caller binding as_of_utc to the intended cutoff.
    """
    runner, study, store = _runner(tmp_path)
    bars = _retrieve(OverreachingProvider(UNIVERSE[:8]), CUTOFF)
    result = runner.run(make_request(study, "M5", NOW), bars)
    assert result.as_of_utc == NOW
    (received,) = study.seen_bars
    assert len(received) == 8
    assert max(bar_end(b, "M5") for b in received) > CUTOFF  # past the pipeline end_utc
    assert all(bar_end(b, "M5") <= result.as_of_utc for b in received)  # not past as_of_utc


def test_b5_bar_opening_exactly_at_end_utc(tmp_path: Path) -> None:
    runner, study, _store = _runner(tmp_path)
    provider = OverreachingProvider(UNIVERSE[:6])
    bars = _retrieve(provider, CUTOFF)
    assert bars.bars[-1].timestamp_utc == provider.requests[0][3] == CUTOFF
    with pytest.raises(StudyRequestError):
        runner.run(make_request(study, "M5", CUTOFF), bars)
    accepted = runner.run(make_request(study, "M5", CUTOFF + DURATIONS["M5"]), bars)
    assert accepted.as_of_utc == CUTOFF + DURATIONS["M5"]


def test_b6_caller_truncation_keeps_future_bars_out_of_compute(tmp_path: Path) -> None:
    def run(universe_returned: tuple, label: str) -> tuple[StudyResult, CausalMean3]:  # type: ignore[type-arg]
        runner, study, _ = _runner(tmp_path / label)
        raw = _retrieve(OverreachingProvider(universe_returned), CUTOFF)
        # The test oracle stands in for the caller-side selection the pipeline does not do.
        kept = select_completed_bars(raw.bars, "M5", CUTOFF)
        bars = ValidatedBarSet.from_bars(kept, "M5", NOW)
        return runner.run(make_request(study, "M5", CUTOFF), bars), study

    (tmp_path / "base").mkdir()
    (tmp_path / "altered").mkdir()
    base, base_study = run(UNIVERSE[:8], "base")
    altered, altered_study = run(alter_future(UNIVERSE, "M5", CUTOFF)[:8], "altered")
    assert base_study.seen_bars == [UNIVERSE[:5]]
    assert altered_study.seen_bars == [UNIVERSE[:5]]
    assert base == altered
    assert dict(base.provenance)["input_window_end_utc"] == "2026-01-01T00:25:00Z"
    assert dict(base.provenance)["input_bar_count"] == "5"
    assert timedelta(0) < NOW - CUTOFF
