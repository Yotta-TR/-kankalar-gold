"""Pipeline-to-study failure mapping uses synthetic providers and data only."""

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import pytest

from kankalar_gold_ai.adapters.mt5.provider import MT5EmptyHistoryError
from kankalar_gold_ai.core.contracts import (
    Direction,
    MarketBar,
    StudyResult,
    StudyStatus,
)
from kankalar_gold_ai.core.study_results import StudyResultKey
from kankalar_gold_ai.data.errors import DataProviderError
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.studies import (
    StudyOutput,
    StudyRegistry,
    StudyRunner,
    StudyRunRequest,
    run_pipeline_study,
)
from kankalar_gold_ai.studies.base import ConfigValue

NOW = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)


class _Study:
    study_id = "trend.synthetic"
    version = "1.0.0"
    required_timeframes = frozenset({"M5"})

    def __init__(self) -> None:
        self.compute_calls = 0

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        return dict(raw)

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        return 1

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        self.compute_calls += 1
        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state="synthetic",
            value=1.0,
            normalized_value=1.0,
            direction=Direction.NEUTRAL,
            uncertainty=0.0,
            evidence=("SYNTHETIC",),
        )


class _Store:
    def __init__(self) -> None:
        self.saved: list[StudyResult] = []

    def save_study_result(self, result: StudyResult) -> None:
        self.saved.append(result)

    def find_cached_study_result(self, key: StudyResultKey) -> StudyResult | None:
        return None


class _Provider:
    def __init__(self, bars: tuple[MarketBar, ...] = ()) -> None:
        self.bars = bars
        self.error: Exception | None = None

    def get_bars(
        self, instrument: str, timeframe: str, start_utc: datetime, end_utc: datetime
    ) -> tuple[MarketBar, ...]:
        if self.error is not None:
            raise self.error
        return self.bars


def _setup() -> tuple[StudyRunner, _Study, _Store]:
    study = _Study()
    registry = StudyRegistry()
    registry.register(study)
    store = _Store()
    return StudyRunner(registry, store), study, store


def _request() -> StudyRunRequest:
    return StudyRunRequest(
        study_id="trend.synthetic",
        version="1.0.0",
        instrument="SYNTHETIC_GOLD",
        timeframe="M5",
        as_of_utc=NOW,
    )


def test_pipeline_success_runs_only_cutoff_validated_bars() -> None:
    bar = MarketBar(
        "SYNTHETIC_GOLD",
        "M5",
        NOW - timedelta(minutes=5),
        100.0,
        102.0,
        99.0,
        101.0,
        None,
        "synthetic",
    )
    runner, study, _store = _setup()
    pipeline = HistoricalDataPipeline(_Provider((bar,)), clock=lambda: NOW)

    result = run_pipeline_study(runner, pipeline, _request(), history_days=1)

    assert result.status is StudyStatus.AVAILABLE
    assert result.data_snapshot_id is not None
    assert study.compute_calls == 1


def test_empty_pipeline_history_maps_to_insufficient_data_without_computation() -> None:
    runner, study, store = _setup()
    pipeline = HistoricalDataPipeline(_Provider(), clock=lambda: NOW)

    result = run_pipeline_study(runner, pipeline, _request(), history_days=1)

    assert result.status is StudyStatus.INSUFFICIENT_DATA
    assert result.evidence == ("DATA_INSUFFICIENT_DATA", "EMPTY_HISTORY")
    assert result.data_snapshot_id is None
    assert dict(result.provenance)["data_checked_at_utc"] == "2026-01-05T10:00:00Z"
    assert study.compute_calls == 0
    assert store.saved == [result]


def test_typed_provider_empty_history_keeps_source_provenance() -> None:
    runner, study, _store = _setup()
    provider = _Provider()
    provider.error = MT5EmptyHistoryError("synthetic empty history")
    pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)

    result = run_pipeline_study(runner, pipeline, _request(), history_days=1)

    assert result.status is StudyStatus.INSUFFICIENT_DATA
    assert result.evidence == (
        "DATA_INSUFFICIENT_DATA",
        "DATA_SOURCE_MT5",
        "EMPTY_HISTORY",
    )
    assert study.compute_calls == 0


def test_provider_failure_maps_to_auditable_error_without_computation() -> None:
    runner, study, _store = _setup()
    provider = _Provider()
    provider.error = DataProviderError(
        "synthetic provider failure",
        source_id="synthetic",
        reason_code="SYNTHETIC_PROVIDER_ERROR",
    )
    pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)

    result = run_pipeline_study(runner, pipeline, _request(), history_days=1)

    assert result.status is StudyStatus.ERROR
    assert result.evidence == (
        "DATA_ERROR",
        "DATA_SOURCE_SYNTHETIC",
        "SYNTHETIC_PROVIDER_ERROR",
    )
    assert result.data_snapshot_id is None
    assert dict(result.provenance)["data_checked_at_utc"] == "2026-01-05T10:00:00Z"
    assert study.compute_calls == 0


def test_unclassified_request_errors_still_propagate() -> None:
    runner, study, _store = _setup()
    provider = _Provider()
    provider.error = ValueError("invalid request")
    pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)

    with pytest.raises(ValueError, match="invalid request"):
        run_pipeline_study(runner, pipeline, _request(), history_days=1)

    assert study.compute_calls == 0
