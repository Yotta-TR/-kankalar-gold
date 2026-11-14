"""Deterministic single-timeframe study runner (research-only; no I/O besides the store)."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Protocol

from kankalar_gold_ai.core.contracts import (
    DataQualityReport,
    DataStatus,
    Direction,
    StudyResult,
    StudyStatus,
)
from kankalar_gold_ai.core.study_results import (
    CACHE_FORMAT_VERSION,
    PROV_BAR_COUNT,
    PROV_CACHE_FORMAT,
    PROV_DATA_CHECKED,
    PROV_SOURCE,
    PROV_STUDY_VERSION,
    PROV_WINDOW_END,
    PROV_WINDOW_START,
    StudyResultKey,
    StudyResultValidationError,
    canonical_inputs_json,
    is_cache_eligible,
    utc_z,
    validate_study_result,
)
from kankalar_gold_ai.data.quality import TIMEFRAME_DURATIONS
from kankalar_gold_ai.data.snapshot import compute_bar_snapshot_id
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.studies.base import ConfigValue, StudyOutput, validate_config_values
from kankalar_gold_ai.studies.errors import (
    StudyExecutionError,
    StudyPersistenceError,
    StudyRequestError,
)
from kankalar_gold_ai.studies.registry import RegisteredStudy, StudyRegistry

_LOGGER = logging.getLogger("kankalar_gold_ai.studies.runner")

_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_MAX_REASONS = 32
_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")

ERROR_EXECUTION = "STUDY_EXECUTION_ERROR"
ERROR_INVALID_OUTPUT = "INVALID_STUDY_OUTPUT"
_ERROR_LIMITATION = "Study computation failed; no value is available for this input."
_WARMUP_LIMITATION = "Fewer bars than the study warm-up minimum were supplied."
_DATA_LIMITATION = "Input data failed quality validation; the study was not computed."


class StudyResultStore(Protocol):
    """Persistence required by the runner; implemented by SQLitePersistence."""

    def save_study_result(self, result: StudyResult) -> None: ...

    def find_cached_study_result(self, key: StudyResultKey) -> StudyResult | None: ...


def _is_aware_datetime(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class StudyRunRequest:
    """Explicit study, market identity, analysis cutoff and raw configuration."""

    study_id: str
    version: str
    instrument: str
    timeframe: str
    as_of_utc: datetime
    config: Mapping[str, ConfigValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("study_id", self.study_id),
            ("version", self.version),
            ("instrument", self.instrument),
            ("timeframe", self.timeframe),
        ):
            if type(value) is not str or not value:
                raise StudyRequestError(f"{name} must be a non-empty built-in string")
        if not _is_aware_datetime(self.as_of_utc):
            raise StudyRequestError("as_of_utc must be a timezone-aware datetime")
        object.__setattr__(self, "as_of_utc", _utc(self.as_of_utc))
        try:
            copied = validate_config_values(self.config)
        except Exception as error:
            raise StudyRequestError("request configuration is invalid") from error
        object.__setattr__(self, "config", MappingProxyType(copied))


@dataclass(frozen=True, slots=True)
class DataFailure:
    """Caller-asserted binding of a data-quality failure to a request.

    ``DataQualityReport`` carries no instrument, timeframe or request identity,
    so this binding cannot be verified by the runner beyond consistency checks.
    """

    instrument: str
    timeframe: str
    as_of_utc: datetime
    report: DataQualityReport


class StudyRunner:
    """Runs one registered study on one validated bar set, or records a data failure."""

    def __init__(self, registry: StudyRegistry, store: StudyResultStore) -> None:
        self._registry = registry
        self._store = store

    # -- shared steps ------------------------------------------------------
    def _resolve(self, request: StudyRunRequest) -> RegisteredStudy:
        if type(request) is not StudyRunRequest:
            raise StudyRequestError("request must be a StudyRunRequest")
        registered = self._registry.get(request.study_id, request.version)
        if registered.required_timeframes != frozenset({request.timeframe}):
            raise StudyRequestError(
                "only single-timeframe studies matching the request timeframe are supported"
            )
        return registered

    @staticmethod
    def _normalize(
        registered: RegisteredStudy, request: StudyRunRequest
    ) -> tuple[dict[str, ConfigValue], int]:
        normalized = dict(registered.validate_config(request.config))
        minimum = registered.min_bars(normalized)
        return normalized, minimum

    def _save(self, result: StudyResult) -> None:
        try:
            self._store.save_study_result(result)
        except Exception as error:
            raise StudyPersistenceError("failed to persist study result") from error

    @staticmethod
    def _validate(result: StudyResult) -> None:
        try:
            validate_study_result(result)
        except StudyResultValidationError as error:
            raise StudyExecutionError("constructed study result is invalid") from error

    # -- public API --------------------------------------------------------
    def run(self, request: StudyRunRequest, bars: ValidatedBarSet) -> StudyResult:
        registered = self._resolve(request)
        if type(bars) is not ValidatedBarSet:
            raise StudyRequestError("bars must be a ValidatedBarSet")
        if bars.instrument != request.instrument or bars.timeframe != request.timeframe:
            raise StudyRequestError("bar set does not match the request instrument/timeframe")
        if bars.cutoff_utc != request.as_of_utc:
            raise StudyRequestError("bar set cutoff does not match the request cutoff")
        duration = TIMEFRAME_DURATIONS.get(request.timeframe)
        if duration is None:
            raise StudyRequestError("unsupported timeframe")
        window_start = bars.bars[0].timestamp_utc
        window_end = max(bar.timestamp_utc for bar in bars.bars) + duration
        if _utc(window_end) > request.as_of_utc:
            raise StudyRequestError("input bars extend past the requested analysis cutoff")
        if request.as_of_utc > _utc(bars.validated_at_utc):
            raise StudyRequestError("analysis cutoff is later than the data validation time")

        normalized, minimum = self._normalize(registered, request)
        try:
            snapshot_id = compute_bar_snapshot_id(bars.bars)
        except Exception as error:
            raise StudyExecutionError("failed to compute the data snapshot ID") from error
        inputs_json = canonical_inputs_json(normalized)
        try:
            key = StudyResultKey(
                study_id=registered.study_id,
                version=registered.version,
                instrument=request.instrument,
                timeframe=request.timeframe,
                as_of_utc=request.as_of_utc,
                inputs_json=inputs_json,
                data_snapshot_id=snapshot_id,
            )
        except StudyResultValidationError as error:
            raise StudyExecutionError("failed to build the cache key") from error

        provenance = tuple(
            sorted(
                {
                    PROV_CACHE_FORMAT: CACHE_FORMAT_VERSION,
                    PROV_STUDY_VERSION: registered.version,
                    PROV_WINDOW_START: utc_z(_utc(window_start)),
                    PROV_WINDOW_END: utc_z(_utc(window_end)),
                    PROV_BAR_COUNT: str(len(bars.bars)),
                    PROV_SOURCE: bars.source,
                }.items()
            )
        )

        try:
            cached = self._store.find_cached_study_result(key)
        except Exception as error:
            raise StudyPersistenceError("cache lookup failed") from error
        if cached is not None:
            return self._accept_cached(cached, key, provenance)

        def build(
            status: StudyStatus,
            output: StudyOutput | None,
            evidence: tuple[str, ...],
            limitations: tuple[str, ...],
        ) -> StudyResult:
            return StudyResult(
                study_id=registered.study_id,
                version=registered.version,
                instrument=request.instrument,
                timeframe=request.timeframe,
                as_of_utc=request.as_of_utc,
                status=status,
                state=None if output is None else output.state,
                value=None if output is None else output.value,
                normalized_value=None if output is None else output.normalized_value,
                direction=Direction.NOT_APPLICABLE if output is None else output.direction,
                uncertainty=None if output is None else output.uncertainty,
                inputs=dict(normalized),
                evidence=evidence,
                limitations=limitations,
                data_snapshot_id=snapshot_id,
                provenance=provenance,
            )

        if len(bars.bars) < minimum:
            result = build(
                StudyStatus.INSUFFICIENT_DATA,
                None,
                ("INSUFFICIENT_BARS",),
                (_WARMUP_LIMITATION,),
            )
        else:
            result = self._compute(registered, bars, normalized, build)
        self._validate(result)
        self._save(result)
        return result

    def _compute(
        self,
        registered: RegisteredStudy,
        bars: ValidatedBarSet,
        normalized: dict[str, ConfigValue],
        build: "_Builder",
    ) -> StudyResult:
        try:
            output: object = registered.compute(bars.bars, normalized)
        except Exception as error:
            self._log_failure(registered, type(error).__name__)
            return build(StudyStatus.ERROR, None, (ERROR_EXECUTION,), (_ERROR_LIMITATION,))
        if type(output) is not StudyOutput:
            self._log_failure(registered, _safe_type_name(output))
            return build(StudyStatus.ERROR, None, (ERROR_INVALID_OUTPUT,), (_ERROR_LIMITATION,))
        return build(output.status, output, output.evidence, output.limitations)

    @staticmethod
    def _log_failure(registered: RegisteredStudy, class_name: str) -> None:
        # Fixed message and sanitized class metadata only: no message, traceback or exc_info.
        try:
            safe = class_name if _CLASS_NAME.fullmatch(class_name) else "<invalid>"
            _LOGGER.error(
                "study execution failed study_id=%s version=%s error_class=%s",
                registered.study_id,
                registered.version,
                safe,
            )
        except Exception:  # noqa: BLE001 - logging must never alter the result
            pass

    @staticmethod
    def _accept_cached(
        cached: StudyResult,
        key: StudyResultKey,
        expected_provenance: tuple[tuple[str, str], ...],
    ) -> StudyResult:
        try:
            validate_study_result(cached)
            matches = (
                cached.study_id == key.study_id
                and cached.version == key.version
                and cached.instrument == key.instrument
                and cached.timeframe == key.timeframe
                and cached.as_of_utc == key.as_of_utc
                and cached.data_snapshot_id == key.data_snapshot_id
                and canonical_inputs_json(cached.inputs) == key.inputs_json
                and cached.provenance == expected_provenance
                and is_cache_eligible(cached)
            )
        except StudyResultValidationError as error:
            raise StudyPersistenceError("cached study result is invalid") from error
        except Exception as error:
            raise StudyPersistenceError("cached study result is invalid") from error
        if not matches:
            raise StudyPersistenceError("cached study result does not match the cache key")
        return cached

    def run_data_failure(self, request: StudyRunRequest, failure: DataFailure) -> StudyResult:
        registered = self._resolve(request)
        report = _validate_failure(request, failure)
        normalized, _minimum = self._normalize(registered, request)
        status = StudyStatus(report.status.value)
        evidence = (f"DATA_{report.status.value}", *report.reasons)
        provenance = tuple(
            sorted(
                {
                    PROV_CACHE_FORMAT: CACHE_FORMAT_VERSION,
                    PROV_STUDY_VERSION: registered.version,
                    PROV_DATA_CHECKED: utc_z(_utc(report.checked_at_utc)),
                }.items()
            )
        )
        result = StudyResult(
            study_id=registered.study_id,
            version=registered.version,
            instrument=request.instrument,
            timeframe=request.timeframe,
            as_of_utc=request.as_of_utc,
            status=status,
            state=None,
            value=None,
            normalized_value=None,
            direction=Direction.NOT_APPLICABLE,
            uncertainty=None,
            inputs=normalized,
            evidence=evidence,
            limitations=(_DATA_LIMITATION,),
            data_snapshot_id=None,
            provenance=provenance,
        )
        self._validate(result)
        self._save(result)
        return result


class _Builder(Protocol):
    def __call__(
        self,
        status: StudyStatus,
        output: StudyOutput | None,
        evidence: tuple[str, ...],
        limitations: tuple[str, ...],
    ) -> StudyResult: ...


def _safe_type_name(value: object) -> str:
    try:
        name = type(value).__name__
    except Exception:  # noqa: BLE001
        return "<invalid>"
    return name if type(name) is str else "<invalid>"


def _validate_failure(request: StudyRunRequest, failure: DataFailure) -> DataQualityReport:
    if type(failure) is not DataFailure:
        raise StudyRequestError("failure must be a DataFailure")
    report = failure.report
    if type(report) is not DataQualityReport:
        raise StudyRequestError("failure report must be a DataQualityReport")
    for name, value in (("instrument", failure.instrument), ("timeframe", failure.timeframe)):
        if type(value) is not str or not value:
            raise StudyRequestError(f"failure {name} must be a non-empty built-in string")
    if not _is_aware_datetime(failure.as_of_utc) or not _is_aware_datetime(report.checked_at_utc):
        raise StudyRequestError("failure timestamps must be timezone-aware datetimes")
    if type(report.status) is not DataStatus or report.status is DataStatus.VALID:
        raise StudyRequestError("failure report status must be a non-VALID DataStatus")
    reasons = report.reasons
    if (
        type(reasons) is not tuple
        or not reasons
        or len(reasons) > _MAX_REASONS
        or any(type(r) is not str or _REASON_CODE.fullmatch(r) is None for r in reasons)
    ):
        raise StudyRequestError("failure report reasons are malformed")
    if failure.instrument != request.instrument or failure.timeframe != request.timeframe:
        raise StudyRequestError("failure does not match the request instrument/timeframe")
    if _utc(failure.as_of_utc) != request.as_of_utc:
        raise StudyRequestError("failure as_of_utc does not match the request cutoff")
    if _utc(report.checked_at_utc) < request.as_of_utc:
        raise StudyRequestError("data quality check precedes the requested cutoff")
    return report


__all__ = [
    "DataFailure",
    "StudyResultStore",
    "StudyRunRequest",
    "StudyRunner",
]
