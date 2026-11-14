"""F4 production boundary: cutoff-bound pipeline, carrier and runner.

Synthetic data, a scripted clock and a fake MT5 client only; no network or live MT5.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
from kankalar_gold_ai.adapters.mt5.provider import MT5MarketDataProvider
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
    append_future,
    bar_end,
    build_bar,
    build_universe,
    end_of,
    make_request,
    select_completed_bars,
)
from tests.test_mt5_adapter import BAR_TIME, SYMBOL, FakeMT5

UNIVERSE = build_universe(10)
CUTOFF = end_of(4)  # 00:25, the intended analysis cutoff
NOW = end_of(9)  # 00:50: every universe bar is complete at validation time


class CountingStore:
    def __init__(self, inner: SQLitePersistence) -> None:
        self.inner = inner
        self.saves = 0
        self.lookups = 0

    def save_study_result(self, result: StudyResult) -> None:
        self.saves += 1
        self.inner.save_study_result(result)

    def find_cached_study_result(self, key: StudyResultKey) -> StudyResult | None:
        self.lookups += 1
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


def test_b1_compliant_provider_returns_bars_completed_by_cutoff(
    tmp_path: Path,
) -> None:
    runner, study, store = _runner(tmp_path)
    bars = _retrieve(MockMarketDataProvider(UNIVERSE), CUTOFF)
    assert bars.bars == select_completed_bars(UNIVERSE, "M5", CUTOFF)
    assert bar_end(bars.bars[-1], "M5") == CUTOFF
    assert runner.run(make_request(study, "M5", CUTOFF), bars).as_of_utc == CUTOFF
    assert study.calls == 1 and store.saves == 1


def test_b2_pipeline_rejects_overreaching_provider() -> None:
    provider = OverreachingProvider(UNIVERSE[:8])
    with pytest.raises(ValueError, match="open-time range"):
        _retrieve(provider, CUTOFF)
    assert provider.requests[0][2:] == (CUTOFF - timedelta(days=1), CUTOFF - DURATIONS["M5"])


def test_b3_direct_carrier_rejects_overlong_set_before_compute(tmp_path: Path) -> None:
    _runner_instance, study, store = _runner(tmp_path)
    with pytest.raises(ValueError, match="complete after"):
        ValidatedBarSet.from_bars(UNIVERSE[:8], "M5", NOW, cutoff_utc=CUTOFF)
    assert study.calls == 0 and study.seen_bars == [] and store.lookups == store.saves == 0


def test_b4_previously_unbound_pipeline_cutoff_now_rejects_later_as_of(
    tmp_path: Path,
) -> None:
    """Regression for the previously characterized pipeline-to-runner binding gap."""
    runner, study, store = _runner(tmp_path)
    bars = _retrieve(MockMarketDataProvider(UNIVERSE), CUTOFF)
    for cutoff in (NOW, CUTOFF - DURATIONS["M5"]):
        with pytest.raises(StudyRequestError, match="cutoff"):
            runner.run(make_request(study, "M5", cutoff), bars)
    assert study.calls == 0 and study.seen_bars == [] and store.lookups == store.saves == 0


def test_b5_provider_bar_opening_exactly_at_cutoff_is_rejected() -> None:
    provider = OverreachingProvider(UNIVERSE[:6])
    assert UNIVERSE[5].timestamp_utc == CUTOFF
    with pytest.raises(ValueError, match="open-time range"):
        _retrieve(provider, CUTOFF)


def test_b6_future_changes_do_not_reach_compute(tmp_path: Path) -> None:
    def run(universe_returned: tuple, label: str) -> tuple[StudyResult, CausalMean3]:  # type: ignore[type-arg]
        runner, study, _ = _runner(tmp_path / label)
        bars = _retrieve(MockMarketDataProvider(universe_returned), CUTOFF)
        return runner.run(make_request(study, "M5", CUTOFF), bars), study

    (tmp_path / "base").mkdir()
    (tmp_path / "altered").mkdir()
    (tmp_path / "appended").mkdir()
    base, base_study = run(UNIVERSE[:8], "base")
    altered, altered_study = run(alter_future(UNIVERSE, "M5", CUTOFF)[:8], "altered")
    appended, appended_study = run(append_future(UNIVERSE, "M5"), "appended")
    assert base_study.seen_bars == [UNIVERSE[:5]]
    assert altered_study.seen_bars == [UNIVERSE[:5]]
    assert appended_study.seen_bars == [UNIVERSE[:5]]
    assert base == altered == appended
    assert dict(base.provenance)["input_window_end_utc"] == "2026-01-01T00:25:00Z"
    assert dict(base.provenance)["input_bar_count"] == "5"
    assert timedelta(0) < NOW - CUTOFF


@pytest.mark.parametrize("timeframe", ["M1", "M5", "H1", "D1"])
@pytest.mark.parametrize("method", ["retrieve_bars", "retrieve_validated_bars"])
def test_provider_open_bounds_and_completion_cutoff(timeframe: str, method: str) -> None:
    duration = DURATIONS[timeframe]
    universe = build_universe(4, timeframe)
    cutoff = end_of(2, timeframe)
    now = end_of(3, timeframe)
    provider = OverreachingProvider(universe[:3])
    pipeline = HistoricalDataPipeline(provider, clock=ScriptedClock([now]))
    result = getattr(pipeline, method)(INSTRUMENT, timeframe, cutoff, 5)
    bars = result.bars if isinstance(result, ValidatedBarSet) else result
    assert bars == universe[:3]
    assert bar_end(bars[-1], timeframe) == cutoff
    assert provider.requests[0][2:] == (cutoff - timedelta(days=5), cutoff - duration)


@pytest.mark.parametrize("method", ["retrieve_bars", "retrieve_validated_bars"])
@pytest.mark.parametrize("side", ["before", "after"])
def test_provider_overreach_fails_closed(method: str, side: str) -> None:
    bar = build_bar(-300, "M5", 100.0) if side == "before" else build_bar(4, "M5", 100.0)
    if side == "after":
        bar = replace(bar, timestamp_utc=bar.timestamp_utc + timedelta(microseconds=1))
    pipeline = _pipeline(OverreachingProvider((bar,)))
    with pytest.raises(ValueError, match="open-time range"):
        getattr(pipeline, method)(INSTRUMENT, "M5", CUTOFF, 1)


def test_pipeline_cutoff_normalization_and_naive_rejection(tmp_path: Path) -> None:
    offset = timezone(timedelta(hours=3))
    provider = OverreachingProvider(UNIVERSE[:5])
    bars = _retrieve(provider, CUTOFF.astimezone(offset))
    assert bars.cutoff_utc == CUTOFF and bars.cutoff_utc.tzinfo is timezone.utc
    assert provider.requests[0][3] == CUTOFF - DURATIONS["M5"]
    runner, study, _ = _runner(tmp_path)
    assert (
        runner.run(make_request(study, "M5", CUTOFF.astimezone(offset)), bars).as_of_utc == CUTOFF
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        _retrieve(MockMarketDataProvider(UNIVERSE), CUTOFF.replace(tzinfo=None))


def test_pipeline_mt5_fake_receives_open_time_range() -> None:
    client = FakeMT5()
    provider = MT5MarketDataProvider(client, SYMBOL, clock=lambda: NOW)
    provider.connect()
    cutoff = BAR_TIME + timedelta(minutes=5)
    pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)
    bars = pipeline.retrieve_validated_bars(SYMBOL, "M5", cutoff, 1)
    assert bars.cutoff_utc == cutoff
    assert bars.bars[-1].timestamp_utc + DURATIONS["M5"] == cutoff
    assert client.rate_args == (SYMBOL, client.TIMEFRAME_M5, cutoff - timedelta(days=1), BAR_TIME)
