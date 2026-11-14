"""StudyRunner tests: deterministic fake studies, synthetic bars, temporary SQLite only."""

from __future__ import annotations

import ast
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kankalar_gold_ai.core.contracts import (
    DataQualityReport,
    DataStatus,
    Direction,
    MarketBar,
    StudyResult,
    StudyStatus,
)
from kankalar_gold_ai.core.study_results import (
    CACHE_FORMAT_VERSION,
    StudyResultKey,
    StudyResultValidationError,
    is_cache_eligible,
    validate_study_result,
)
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import SQLitePersistence
from kankalar_gold_ai.studies import (
    DataFailure,
    StudyConfigError,
    StudyOutput,
    StudyPersistenceError,
    StudyRegistry,
    StudyRequestError,
    StudyRunner,
    StudyRunRequest,
    UnknownStudyError,
)
from kankalar_gold_ai.studies.base import ConfigValue
from tests.fixtures.market_data import SYNTHETIC_BARS

NOW = datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[1] / "src" / "kankalar_gold_ai"


class RunnerFakeStudy:
    study_id = "trend.fake"
    version = "1.0.0"
    required_timeframes = frozenset({"M5"})

    def __init__(self, minimum: int = 2, behavior: str = "ok") -> None:
        self.minimum = minimum
        self.behavior = behavior
        self.compute_calls = 0
        self.seen_config: Mapping[str, ConfigValue] | None = None

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        if raw.get("bad"):
            raise StudyConfigError("bad")
        return {**raw, "normalized": True}

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        return self.minimum

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        self.compute_calls += 1
        self.seen_config = config
        if self.behavior == "raise":
            raise RuntimeError("secret-token-123")
        if self.behavior == "invalid":
            return "nope"  # type: ignore[return-value]
        if self.behavior == "base":
            raise KeyboardInterrupt
        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state="up",
            value=1.5,
            normalized_value=0.5,
            direction=Direction.BULLISH,
            uncertainty=0.1,
            evidence=("ok",),
        )


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


def _setup(tmp_path: Path, **kwargs: object) -> tuple[StudyRunner, RunnerFakeStudy, CountingStore]:
    study = RunnerFakeStudy(**kwargs)  # type: ignore[arg-type]
    registry = StudyRegistry()
    registry.register(study)
    inner = SQLitePersistence(tmp_path / "r.db")
    inner.initialize()
    store = CountingStore(inner)
    return StudyRunner(registry, store), study, store


def _request(**changes: object) -> StudyRunRequest:
    values: dict[str, object] = {
        "study_id": "trend.fake",
        "version": "1.0.0",
        "instrument": "TEST_GOLD",
        "timeframe": "M5",
        "as_of_utc": NOW,
        "config": {"period": 3},
    }
    values.update(changes)
    return StudyRunRequest(**values)  # type: ignore[arg-type]


def _bars(count: int = 3, validated_at: datetime = NOW, cutoff: datetime = NOW) -> ValidatedBarSet:
    return ValidatedBarSet.from_bars(SYNTHETIC_BARS[:count], "M5", validated_at, cutoff_utc=cutoff)


def test_success_result_provenance_and_inputs(tmp_path: Path) -> None:
    runner, study, store = _setup(tmp_path)
    result = runner.run(_request(), _bars())
    assert result.status is StudyStatus.AVAILABLE
    assert result.as_of_utc == NOW
    assert result.inputs == {"period": 3, "normalized": True}
    assert result.data_snapshot_id is not None
    prov = dict(result.provenance)
    assert prov["cache_format_version"] == CACHE_FORMAT_VERSION
    assert prov["study_version"] == "1.0.0"
    assert prov["input_window_start_utc"] == "2026-01-01T00:00:00Z"
    assert prov["input_window_end_utc"] == "2026-01-01T00:15:00Z"
    assert prov["input_bar_count"] == "3"
    assert "provenance" not in (study.seen_config or {})
    assert store.saves == 1
    validate_study_result(result)


def test_cache_hit_does_not_compute_or_save_again(tmp_path: Path) -> None:
    runner, study, store = _setup(tmp_path)
    first = runner.run(_request(), _bars())
    second = runner.run(_request(), _bars())
    assert second == first
    assert study.compute_calls == 1 and store.saves == 1


def test_different_cutoffs_never_share_cache(tmp_path: Path) -> None:
    runner, study, store = _setup(tmp_path)
    later = NOW + timedelta(minutes=15)
    first = runner.run(_request(), _bars(validated_at=later))
    second = runner.run(_request(as_of_utc=later), _bars(validated_at=later, cutoff=later))
    assert first.as_of_utc == NOW and second.as_of_utc == later
    assert study.compute_calls == 2 and store.saves == 2


def test_config_snapshot_and_version_change_cache_miss(tmp_path: Path) -> None:
    runner, study, _store = _setup(tmp_path)
    runner.run(_request(), _bars())
    runner.run(_request(config={"period": 4}), _bars())
    runner.run(_request(), _bars(count=2))
    assert study.compute_calls == 3


def test_warmup_result_is_snapshot_bound_and_cached(tmp_path: Path) -> None:
    runner, study, store = _setup(tmp_path, minimum=5)
    first = runner.run(_request(), _bars())
    second = runner.run(_request(), _bars())
    assert first.status is StudyStatus.INSUFFICIENT_DATA
    assert first.data_snapshot_id is not None
    assert first.direction is Direction.NOT_APPLICABLE
    assert second == first and store.saves == 1 and study.compute_calls == 0


def test_exact_warmup_minimum_computes(tmp_path: Path) -> None:
    runner, study, _store = _setup(tmp_path, minimum=3)
    assert runner.run(_request(), _bars()).status is StudyStatus.AVAILABLE
    assert study.compute_calls == 1


def test_compute_exception_returns_fixed_error_and_is_not_cached(tmp_path: Path) -> None:
    runner, study, store = _setup(tmp_path, behavior="raise")
    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("kankalar_gold_ai.studies.runner")
    handler = Collector()
    logger.addHandler(handler)
    try:
        result = runner.run(_request(), _bars())
        runner.run(_request(), _bars())
    finally:
        logger.removeHandler(handler)
    text = " ".join(record.getMessage() for record in records)
    assert result.status is StudyStatus.ERROR
    assert result.evidence == ("STUDY_EXECUTION_ERROR",)
    assert result.state is None and result.value is None
    assert "secret-token-123" not in text
    assert "RuntimeError" in text
    assert all(record.exc_info is None for record in records)
    assert store.saves == 2 and study.compute_calls == 2
    assert not is_cache_eligible(result)


def test_invalid_output_becomes_error_result(tmp_path: Path) -> None:
    runner, _study, store = _setup(tmp_path, behavior="invalid")
    result = runner.run(_request(), _bars())
    assert result.status is StudyStatus.ERROR
    assert result.evidence == ("INVALID_STUDY_OUTPUT",)
    assert store.saves == 1


def test_base_exception_propagates(tmp_path: Path) -> None:
    runner, _study, store = _setup(tmp_path, behavior="base")
    with pytest.raises(KeyboardInterrupt):
        runner.run(_request(), _bars())
    assert store.saves == 0


def test_resolution_and_request_errors(tmp_path: Path) -> None:
    runner, study, store = _setup(tmp_path)
    with pytest.raises(UnknownStudyError):
        runner.run(_request(study_id="missing.study"), _bars())
    with pytest.raises(UnknownStudyError):
        runner.run(_request(version="9.9.9"), _bars())
    with pytest.raises(StudyRequestError):
        runner.run(_request(instrument="OTHER"), _bars())
    with pytest.raises(StudyRequestError):
        runner.run(_request(timeframe="M1"), _bars())
    with pytest.raises(StudyRequestError):
        runner.run(_request(), SYNTHETIC_BARS)  # type: ignore[arg-type]
    with pytest.raises(StudyConfigError):
        runner.run(_request(config={"bad": True}), _bars())
    assert study.compute_calls == 0 and store.saves == 0


def test_multi_timeframe_study_is_rejected(tmp_path: Path) -> None:
    runner, study, _store = _setup(tmp_path)
    object.__setattr__(
        runner._registry.get("trend.fake", "1.0.0"), "required_timeframes", frozenset({"M5", "H1"})
    )
    with pytest.raises(StudyRequestError):
        runner.run(_request(), _bars())
    assert study.compute_calls == 0


def test_request_validation() -> None:
    with pytest.raises(StudyRequestError):
        _request(as_of_utc=datetime(2026, 1, 1, 0, 15))
    with pytest.raises(StudyRequestError):
        _request(study_id="")
    with pytest.raises(StudyRequestError):
        _request(config={"x": [1]})
    original = {"period": 3}
    request = _request(config=original)
    original["period"] = 99
    assert request.config["period"] == 3
    with pytest.raises(TypeError):
        request.config["period"] = 1  # type: ignore[index]
    offset = timezone(timedelta(hours=3))
    assert _request(as_of_utc=NOW.astimezone(offset)).as_of_utc == NOW


def test_cutoff_rules(tmp_path: Path) -> None:
    runner, study, store = _setup(tmp_path)
    bars = _bars(validated_at=NOW + timedelta(minutes=5))
    assert runner.run(_request(), bars).as_of_utc == NOW
    assert study.compute_calls == 1 and store.lookups == 1 and store.saves == 1
    for cutoff in (NOW - timedelta(minutes=5), NOW + timedelta(minutes=5)):
        with pytest.raises(StudyRequestError, match="cutoff"):
            runner.run(_request(as_of_utc=cutoff), bars)
    assert study.compute_calls == 1 and store.lookups == 1 and store.saves == 1
    offset = timezone(timedelta(hours=3))
    assert runner.run(_request(as_of_utc=NOW.astimezone(offset)), bars).as_of_utc == NOW
    assert study.compute_calls == 1 and store.lookups == 2 and store.saves == 1


def test_cutoff_defense_in_depth_rejects_corrupted_carriers(tmp_path: Path) -> None:
    runner, study, store = _setup(tmp_path)
    bars = _bars()
    object.__setattr__(bars, "cutoff_utc", NOW - timedelta(minutes=5))
    with pytest.raises(StudyRequestError, match="extend past"):
        runner.run(_request(as_of_utc=bars.cutoff_utc), bars)

    bars = _bars()
    object.__setattr__(bars, "validated_at_utc", NOW - timedelta(minutes=5))
    with pytest.raises(StudyRequestError, match="validation time"):
        runner.run(_request(), bars)
    assert study.compute_calls == store.lookups == store.saves == 0


def test_mutating_caller_config_does_not_change_study_copy(tmp_path: Path) -> None:
    runner, study, _store = _setup(tmp_path)
    raw = {"period": 3}
    request = _request(config=raw)
    runner.run(request, _bars())
    raw["period"] = 7
    assert study.seen_config is not None and study.seen_config["period"] == 3


# -- data failure ----------------------------------------------------------
def _failure(
    status: DataStatus = DataStatus.INSUFFICIENT_DATA,
    reasons: object = ("MISSING_BAR",),
    **changes: object,
) -> DataFailure:
    report = DataQualityReport(status, NOW, reasons)  # type: ignore[arg-type]
    values: dict[str, object] = {
        "instrument": "TEST_GOLD",
        "timeframe": "M5",
        "as_of_utc": NOW,
        "report": report,
    }
    values.update(changes)
    return DataFailure(**values)  # type: ignore[arg-type]


def test_data_failure_result_is_persisted_without_lookup(tmp_path: Path) -> None:
    runner, study, store = _setup(tmp_path)
    result = runner.run_data_failure(_request(), _failure())
    assert result.status is StudyStatus.INSUFFICIENT_DATA
    assert result.data_snapshot_id is None
    assert result.as_of_utc == NOW
    assert result.inputs == {"period": 3, "normalized": True}
    assert result.evidence == ("DATA_INSUFFICIENT_DATA", "MISSING_BAR")
    assert dict(result.provenance)["data_checked_at_utc"] == "2026-01-01T00:15:00Z"
    assert store.saves == 1 and store.lookups == 0 and study.compute_calls == 0
    validate_study_result(result)


@pytest.mark.parametrize("status", [DataStatus.STALE, DataStatus.UNAVAILABLE, DataStatus.ERROR])
def test_data_failure_status_mapping(tmp_path: Path, status: DataStatus) -> None:
    runner, _study, _store = _setup(tmp_path)
    assert runner.run_data_failure(_request(), _failure(status)).status.value == status.value


def test_data_failure_never_hides_configuration_or_request_errors(tmp_path: Path) -> None:
    runner, _study, store = _setup(tmp_path)
    with pytest.raises(StudyConfigError):
        runner.run_data_failure(_request(config={"bad": True}), _failure())
    with pytest.raises(UnknownStudyError):
        runner.run_data_failure(_request(version="2.0.0"), _failure())
    with pytest.raises(StudyRequestError):
        runner.run_data_failure(_request(timeframe="M1"), _failure(timeframe="M1"))
    assert store.saves == 0


def test_data_failure_binding_and_boundaries(tmp_path: Path) -> None:
    runner, _study, store = _setup(tmp_path)
    for bad in (
        _failure(instrument="OTHER"),
        _failure(timeframe="M1"),
        _failure(as_of_utc=NOW + timedelta(minutes=1)),
        _failure(as_of_utc=datetime(2026, 1, 1, 0, 15)),
        _failure(DataStatus.VALID, ()),
        _failure(reasons=()),
        _failure(reasons=["MISSING_BAR"]),
        _failure(reasons=("missing bar",)),
        _failure(reasons=("",)),
        _failure(reasons=("A" * 65,)),
        _failure(reasons=tuple(f"R{i}" for i in range(33))),
        _failure(report=DataQualityReport(DataStatus.ERROR, NOW - timedelta(seconds=1), ("X",))),
        _failure(report="report"),
    ):
        with pytest.raises(StudyRequestError):
            runner.run_data_failure(_request(), bad)
    with pytest.raises(StudyRequestError):
        runner.run_data_failure(_request(), "failure")  # type: ignore[arg-type]
    assert store.saves == 0
    boundary = _failure(report=DataQualityReport(DataStatus.ERROR, NOW, ("X",)))
    assert runner.run_data_failure(_request(), boundary).status is StudyStatus.ERROR


def test_data_failure_rejects_subclasses_and_bad_status(tmp_path: Path) -> None:
    runner, _study, _store = _setup(tmp_path)

    class SubFailure(DataFailure):
        pass

    sub = SubFailure("TEST_GOLD", "M5", NOW, DataQualityReport(DataStatus.ERROR, NOW, ("X",)))
    with pytest.raises(StudyRequestError):
        runner.run_data_failure(_request(), sub)
    report = DataQualityReport("ERROR", NOW, ("X",))  # type: ignore[arg-type]
    with pytest.raises(StudyRequestError):
        runner.run_data_failure(_request(), _failure(report=report))


# -- store failures --------------------------------------------------------
class BrokenStore:
    def __init__(self, lookup: bool = False, save: bool = False) -> None:
        self.lookup, self.save = lookup, save

    def save_study_result(self, result: StudyResult) -> None:
        if self.save:
            raise OSError("disk")

    def find_cached_study_result(self, key: StudyResultKey) -> StudyResult | None:
        if self.lookup:
            raise OSError("disk")
        return None


def test_store_failures_are_wrapped(tmp_path: Path) -> None:
    study = RunnerFakeStudy()
    registry = StudyRegistry()
    registry.register(study)
    with pytest.raises(StudyPersistenceError) as lookup_error:
        StudyRunner(registry, BrokenStore(lookup=True)).run(_request(), _bars())
    assert isinstance(lookup_error.value.__cause__, OSError)
    assert study.compute_calls == 0
    with pytest.raises(StudyPersistenceError):
        StudyRunner(registry, BrokenStore(save=True)).run(_request(), _bars())


def test_runner_rejects_cache_hit_that_violates_key_or_contract(tmp_path: Path) -> None:
    runner, _study, store = _setup(tmp_path)
    good = runner.run(_request(), _bars())

    class Lying:
        def __init__(self, result: StudyResult) -> None:
            self.result = result

        def save_study_result(self, result: StudyResult) -> None:
            pass

        def find_cached_study_result(self, key: StudyResultKey) -> StudyResult | None:
            return self.result

    registry = runner._registry
    for bad in (
        replace(good, instrument="OTHER"),
        replace(good, as_of_utc=NOW - timedelta(minutes=5)),
        replace(
            good,
            status=StudyStatus.ERROR,
            direction=Direction.NOT_APPLICABLE,
            state=None,
            value=None,
            normalized_value=None,
            uncertainty=None,
        ),
        replace(good, provenance=()),
    ):
        with pytest.raises(StudyPersistenceError):
            StudyRunner(registry, Lying(bad)).run(_request(), _bars())
    assert store.saves == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_bar_count": "999"},
        {"input_source": "wrong-source"},
        {"input_window_start_utc": "2026-01-01T00:05:00Z"},
        {"input_window_end_utc": "2026-01-01T00:10:00Z"},
        {"study_version": "2.0.0"},
        {"cache_format_version": "study-result-v0"},
    ],
)
def test_cache_hit_with_mismatched_provenance_is_rejected(
    tmp_path: Path, overrides: dict[str, str]
) -> None:
    runner, study, store = _setup(tmp_path)
    good = runner.run(_request(), _bars())
    mapping = dict(good.provenance)
    mapping.update(overrides)
    forged = replace(good, provenance=tuple(sorted(mapping.items())))

    class Forged:
        def save_study_result(self, result: StudyResult) -> None:
            raise AssertionError("must not save")

        def find_cached_study_result(self, key: StudyResultKey) -> StudyResult | None:
            return forged

    with pytest.raises(StudyPersistenceError):
        StudyRunner(runner._registry, Forged()).run(_request(), _bars())
    assert study.compute_calls == 1 and store.saves == 1


def test_matching_provenance_cache_hit_is_returned(tmp_path: Path) -> None:
    runner, _study, _store = _setup(tmp_path)
    good = runner.run(_request(), _bars())
    assert runner.run(_request(), _bars()).provenance == good.provenance


# -- status matrix and provenance semantics -------------------------------
def _valid_result(tmp_path: Path) -> StudyResult:
    runner, _study, _store = _setup(tmp_path)
    return runner.run(_request(), _bars())


@pytest.mark.parametrize(
    "status",
    [StudyStatus.INSUFFICIENT_DATA, StudyStatus.UNAVAILABLE, StudyStatus.STALE, StudyStatus.ERROR],
)
@pytest.mark.parametrize(
    "changes",
    [
        {"state": "x"},
        {"value": 1.0},
        {"normalized_value": 0.5},
        {"uncertainty": 0.1},
        {"direction": Direction.BULLISH},
    ],
)
def test_non_available_status_matrix(
    tmp_path: Path, status: StudyStatus, changes: dict[str, object]
) -> None:
    base = replace(
        _valid_result(tmp_path),
        status=status,
        state=None,
        value=None,
        normalized_value=None,
        uncertainty=None,
        direction=Direction.NOT_APPLICABLE,
    )
    validate_study_result(base)
    with pytest.raises(StudyResultValidationError):
        validate_study_result(replace(base, **changes))


def test_result_validation_rejects_bad_fields(tmp_path: Path) -> None:
    good = _valid_result(tmp_path)
    for bad in (
        replace(good, value=float("nan")),
        replace(good, value=float("inf")),
        replace(good, data_snapshot_id=None),
        replace(good, inputs={"a": [1]}),
        replace(good, evidence=["x"]),  # type: ignore[arg-type]
    ):
        with pytest.raises(StudyResultValidationError):
            validate_study_result(bad)


def _with_prov(result: StudyResult, **overrides: str | None) -> StudyResult:
    mapping = dict(result.provenance)
    for key, value in overrides.items():
        if value is None:
            mapping.pop(key, None)
        else:
            mapping[key] = value
    return replace(result, provenance=tuple(sorted(mapping.items())))


@pytest.mark.parametrize(
    "overrides",
    [
        {"cache_format_version": "old"},
        {"cache_format_version": None},
        {"study_version": "2.0.0"},
        {"input_window_start_utc": None},
        {"input_window_start_utc": "2026-01-01 00:00:00"},
        {"input_window_start_utc": "2026-01-01T00:20:00Z"},
        {"input_window_end_utc": "2026-01-01T00:30:00Z"},
        {"input_bar_count": "0"},
        {"input_bar_count": "-1"},
        {"input_bar_count": "x"},
        {"input_source": ""},
        {"input_source": None},
        {"data_checked_at_utc": "2026-01-01T00:15:00Z"},
        {"unknown_key": "1"},
    ],
)
def test_snapshot_provenance_semantics(tmp_path: Path, overrides: dict[str, str | None]) -> None:
    good = _valid_result(tmp_path)
    validate_study_result(good)
    with pytest.raises(StudyResultValidationError):
        validate_study_result(_with_prov(good, **overrides))


def test_provenance_shape_and_failure_provenance(tmp_path: Path) -> None:
    good = _valid_result(tmp_path)
    with pytest.raises(StudyResultValidationError):
        validate_study_result(replace(good, provenance=good.provenance + (good.provenance[0],)))
    with pytest.raises(StudyResultValidationError):
        validate_study_result(replace(good, provenance=tuple(reversed(good.provenance))))
    runner, _study, _store = _setup(tmp_path)
    failure = runner.run_data_failure(_request(), _failure())
    validate_study_result(failure)
    for bad in (
        _with_prov(failure, data_checked_at_utc=None),
        _with_prov(failure, input_bar_count="3"),
        _with_prov(failure, input_source="s"),
        _with_prov(failure, data_checked_at_utc="2026-01-01T00:14:59Z"),
        replace(failure, data_snapshot_id="bars-v1:sha256:" + "0" * 64),
    ):
        with pytest.raises(StudyResultValidationError):
            validate_study_result(bad)


def test_cache_eligibility(tmp_path: Path) -> None:
    good = _valid_result(tmp_path)
    assert is_cache_eligible(good)
    assert not is_cache_eligible(replace(good, status=StudyStatus.STALE))
    assert not is_cache_eligible(replace(good, data_snapshot_id=None))


def test_key_requires_snapshot_and_normalizes_cutoff() -> None:
    offset = timezone(timedelta(hours=3))
    key = StudyResultKey("s", "1", "I", "M5", NOW.astimezone(offset), "{}", "snap")
    assert key.as_of_utc == NOW
    with pytest.raises(StudyResultValidationError):
        StudyResultKey("s", "1", "I", "M5", NOW, "{}", "")
    with pytest.raises(StudyResultValidationError):
        StudyResultKey("s", "1", "I", "M5", NOW, "{}", "snap", "other")


# -- import graph / safety -------------------------------------------------
def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_import_direction_has_no_cycles() -> None:
    core = _imports(ROOT / "core" / "study_results.py")
    assert all(
        not name.startswith(
            (
                "kankalar_gold_ai.studies",
                "kankalar_gold_ai.storage",
                "kankalar_gold_ai.data",
                "kankalar_gold_ai.adapters",
            )
        )
        for name in core
    )
    for path in (ROOT / "storage").glob("*.py"):
        assert not any(n.startswith("kankalar_gold_ai.studies") for n in _imports(path))
    for path in (ROOT / "core").glob("*.py"):
        assert not any(
            n.startswith(("kankalar_gold_ai.studies", "kankalar_gold_ai.storage"))
            for n in _imports(path)
        )
    for path in (ROOT / "studies").glob("*.py"):
        assert not any(n.startswith("kankalar_gold_ai.storage") for n in _imports(path))


def test_runner_has_no_network_execution_or_telegram_imports() -> None:
    forbidden = ("socket", "requests", "urllib", "http", "MetaTrader5", "telegram", "asyncio")
    imported = _imports(ROOT / "studies" / "runner.py")
    assert not any(name.split(".")[0] in forbidden for name in imported)
    assert "except BaseException" not in (ROOT / "studies" / "runner.py").read_text()


def test_concurrent_duplicate_misses_are_documented() -> None:
    text = (Path(__file__).parents[1] / "docs" / "DECISIONS.md").read_text()
    assert "duplicate" in text.lower() and "StudyRunner" in text
