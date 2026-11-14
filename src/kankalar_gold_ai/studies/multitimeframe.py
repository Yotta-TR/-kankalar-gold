"""Contracts and deterministic preflight for independent multi-timeframe runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

from kankalar_gold_ai.core.contracts import DataStatus, StudyResult, StudyStatus, as_utc
from kankalar_gold_ai.core.study_results import (
    canonical_inputs_json,
    utc_z,
    validate_study_result,
)
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.studies.base import ConfigValue, validate_config_values
from kankalar_gold_ai.studies.errors import StudyConfigError, StudyRequestError
from kankalar_gold_ai.studies.runner import (
    DataFailure,
    PreparedStudyRun,
    StudyRunner,
    StudyRunRequest,
)

SUPPORTED_TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1")
_ONE_DURATION_MAX_AGES = MappingProxyType(
    {
        "M1": timedelta(minutes=1),
        "M5": timedelta(minutes=5),
        "M15": timedelta(minutes=15),
        "H1": timedelta(hours=1),
        "H4": timedelta(hours=4),
        "D1": timedelta(days=1),
    }
)
COORDINATOR_ID = "multitimeframe.analysis"
COORDINATOR_VERSION = "0.1.0"
FRESHNESS_POLICY_ID = "one-timeframe-duration"
FRESHNESS_POLICY_VERSION = "1.0.0"
INPUT_IDENTITY_VERSION = "analysis-input-v1"
RESULT_IDENTITY_VERSION = "analysis-result-v1"

TimeframeInput: TypeAlias = ValidatedBarSet | DataFailure


def _require_text(name: str, value: object) -> None:
    if type(value) is not str or not value:
        raise StudyRequestError(f"{name} must be a non-empty built-in string")


def _duration_microseconds(value: timedelta) -> int:
    return (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds


def _canonical_json(payload: object) -> str:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise StudyRequestError("analysis manifest is not canonically serializable") from error


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    """The versioned initial policy: each timeframe is fresh for one duration."""

    policy_id: str
    version: str
    max_age_by_timeframe: Mapping[str, timedelta]

    def __post_init__(self) -> None:
        if (
            type(self.policy_id) is not str
            or type(self.version) is not str
            or self.policy_id != FRESHNESS_POLICY_ID
            or self.version != FRESHNESS_POLICY_VERSION
        ):
            raise StudyRequestError("unsupported freshness policy identity")
        if not isinstance(self.max_age_by_timeframe, Mapping):
            raise StudyRequestError("max_age_by_timeframe must be a mapping")
        copied = dict(self.max_age_by_timeframe)
        if any(type(timeframe) is not str for timeframe in copied):
            raise StudyRequestError("freshness policy timeframe keys must be built-in strings")
        if set(copied) != set(SUPPORTED_TIMEFRAMES):
            raise StudyRequestError("freshness policy must configure exactly the six timeframes")
        for timeframe in SUPPORTED_TIMEFRAMES:
            age = copied[timeframe]
            if type(age) is not timedelta or age != _ONE_DURATION_MAX_AGES[timeframe]:
                raise StudyRequestError(
                    "freshness policy maximum age must equal one timeframe duration"
                )
        object.__setattr__(
            self,
            "max_age_by_timeframe",
            MappingProxyType({timeframe: copied[timeframe] for timeframe in SUPPORTED_TIMEFRAMES}),
        )

    @classmethod
    def one_timeframe_duration(cls) -> FreshnessPolicy:
        """Create the approved fixed initial policy with its full effective configuration."""
        return cls(
            FRESHNESS_POLICY_ID,
            FRESHNESS_POLICY_VERSION,
            dict(_ONE_DURATION_MAX_AGES),
        )

    def canonical_configuration(self) -> dict[str, int]:
        """Return effective ages as exact integer microseconds for canonical manifests."""
        return {
            timeframe: _duration_microseconds(self.max_age_by_timeframe[timeframe])
            for timeframe in SUPPORTED_TIMEFRAMES
        }


@dataclass(frozen=True, slots=True)
class StudyExecutionSpec:
    """One explicit single-timeframe study invocation in an analysis request."""

    study_id: str
    version: str
    timeframe: str
    config: Mapping[str, ConfigValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text("study_id", self.study_id)
        _require_text("version", self.version)
        if type(self.timeframe) is not str or self.timeframe not in SUPPORTED_TIMEFRAMES:
            raise StudyRequestError("execution timeframe is unsupported")
        try:
            copied = validate_config_values(self.config)
        except StudyConfigError as error:
            raise StudyRequestError("execution configuration is invalid") from error
        object.__setattr__(self, "config", MappingProxyType(copied))


@dataclass(frozen=True, slots=True)
class MultiTimeframeAnalysisRequest:
    """Common instrument/cutoff, explicit executions and exactly one input per timeframe."""

    instrument: str
    as_of_utc: datetime
    executions: tuple[StudyExecutionSpec, ...]
    inputs_by_timeframe: Mapping[str, TimeframeInput]
    freshness_policy: FreshnessPolicy

    def __post_init__(self) -> None:
        _require_text("instrument", self.instrument)
        if type(self.as_of_utc) is not datetime or self.as_of_utc.tzinfo is None:
            raise StudyRequestError("as_of_utc must be a timezone-aware datetime")
        try:
            cutoff = as_utc(self.as_of_utc)
        except (TypeError, ValueError) as error:
            raise StudyRequestError("as_of_utc must be a timezone-aware datetime") from error
        object.__setattr__(self, "as_of_utc", cutoff)
        if type(self.executions) is not tuple or not self.executions:
            raise StudyRequestError("executions must be a non-empty tuple")
        if any(type(execution) is not StudyExecutionSpec for execution in self.executions):
            raise StudyRequestError("executions must contain StudyExecutionSpec values")
        if type(self.freshness_policy) is not FreshnessPolicy:
            raise StudyRequestError("freshness_policy must be a FreshnessPolicy")
        if not isinstance(self.inputs_by_timeframe, Mapping):
            raise StudyRequestError("inputs_by_timeframe must be a mapping")

        requested = {execution.timeframe for execution in self.executions}
        inputs = dict(self.inputs_by_timeframe)
        for timeframe, input_data in inputs.items():
            if type(timeframe) is not str or timeframe not in SUPPORTED_TIMEFRAMES:
                raise StudyRequestError("input mapping contains an unsupported timeframe key")
            if type(input_data) not in (ValidatedBarSet, DataFailure):
                raise StudyRequestError(
                    "each timeframe input must be exactly one ValidatedBarSet or DataFailure"
                )
            if type(input_data) is ValidatedBarSet:
                if (
                    input_data.timeframe != timeframe
                    or input_data.instrument != self.instrument
                    or input_data.cutoff_utc != cutoff
                ):
                    raise StudyRequestError(
                        "validated input does not match its key/instrument/cutoff"
                    )
            else:
                assert type(input_data) is DataFailure
                if type(input_data.as_of_utc) is not datetime:
                    raise StudyRequestError("data failure does not match its key/instrument/cutoff")
                try:
                    failure_cutoff = as_utc(input_data.as_of_utc)
                except (TypeError, ValueError) as error:
                    raise StudyRequestError(
                        "data failure does not match its key/instrument/cutoff"
                    ) from error
                if (
                    input_data.timeframe != timeframe
                    or input_data.instrument != self.instrument
                    or failure_cutoff != cutoff
                ):
                    raise StudyRequestError("data failure does not match its key/instrument/cutoff")
        if set(inputs) != requested:
            raise StudyRequestError("input timeframe keys must exactly match requested timeframes")
        object.__setattr__(self, "inputs_by_timeframe", MappingProxyType(inputs))


class PreflightEligibility(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class PreflightComponent:
    """Normalized execution metadata and point-in-time eligibility, before study execution."""

    execution: StudyExecutionSpec
    prepared: PreparedStudyRun
    eligibility: PreflightEligibility
    max_age: timedelta
    bar_age: timedelta | None

    def __post_init__(self) -> None:
        if type(self.execution) is not StudyExecutionSpec:
            raise StudyRequestError("preflight execution must be a StudyExecutionSpec")
        if type(self.prepared) is not PreparedStudyRun:
            raise StudyRequestError("preflight prepared run must be a PreparedStudyRun")
        if type(self.eligibility) is not PreflightEligibility:
            raise StudyRequestError("preflight eligibility is invalid")
        if type(self.max_age) is not timedelta or self.max_age <= timedelta(0):
            raise StudyRequestError("preflight max_age must be positive")
        if self.bar_age is not None and type(self.bar_age) is not timedelta:
            raise StudyRequestError("preflight bar_age must be a timedelta or None")
        if self.max_age != _ONE_DURATION_MAX_AGES[self.execution.timeframe]:
            raise StudyRequestError("preflight max_age does not match the fixed freshness policy")
        if (
            self.execution.study_id != self.prepared.study_id
            or self.execution.version != self.prepared.version
            or self.execution.timeframe != self.prepared.request.timeframe
        ):
            raise StudyRequestError("preflight execution does not match its prepared run")
        if self.prepared.request.config != self.execution.config:
            raise StudyRequestError("prepared configuration does not match its execution")
        if self.prepared.data_status is DataStatus.VALID:
            if self.bar_age is None:
                raise StudyRequestError("valid preflight input requires a computed bar age")
            if self.bar_age < timedelta(0):
                raise StudyRequestError("preflight bar age must not be negative")
            expected = (
                PreflightEligibility.STALE
                if self.bar_age > self.max_age
                else (
                    PreflightEligibility.INSUFFICIENT_DATA
                    if self.prepared.input_bar_count is not None
                    and self.prepared.input_bar_count < self.prepared.min_bars
                    else PreflightEligibility.ELIGIBLE
                )
            )
        else:
            expected = PreflightEligibility(self.prepared.data_status.value)
            if self.bar_age is not None:
                raise StudyRequestError("failed preflight input must not have a bar age")
        if self.eligibility is not expected:
            raise StudyRequestError("preflight eligibility does not match prepared input")

    @property
    def timeframe(self) -> str:
        return self.execution.timeframe


@dataclass(frozen=True, slots=True)
class MultiTimeframePreflight:
    """Complete, deterministic input validation; it contains no executed study results."""

    request: MultiTimeframeAnalysisRequest
    source_identity: str | None
    components: tuple[PreflightComponent, ...]
    input_manifest_json: str
    input_snapshot_id: str

    def __post_init__(self) -> None:
        if type(self.request) is not MultiTimeframeAnalysisRequest:
            raise StudyRequestError("preflight request is invalid")
        if type(self.components) is not tuple or not self.components:
            raise StudyRequestError("preflight components must be a non-empty tuple")
        if any(type(component) is not PreflightComponent for component in self.components):
            raise StudyRequestError("preflight components contain an invalid value")
        if len(self.components) != len(self.request.executions):
            raise StudyRequestError("preflight must prepare every requested execution")
        remaining_executions = list(self.request.executions)
        execution_identities: set[tuple[str, str, str, str]] = set()
        for component in self.components:
            try:
                remaining_executions.remove(component.execution)
            except ValueError as error:
                raise StudyRequestError("preflight contains an unrequested execution") from error
            prepared_request = component.prepared.request
            if (
                prepared_request.instrument != self.request.instrument
                or prepared_request.as_of_utc != self.request.as_of_utc
                or prepared_request.timeframe != component.timeframe
            ):
                raise StudyRequestError("preflight run does not match the common analysis identity")
            identity = (
                component.prepared.study_id,
                component.prepared.version,
                component.timeframe,
                canonical_inputs_json(component.prepared.normalized_config),
            )
            if identity in execution_identities:
                raise StudyRequestError("preflight contains duplicate normalized executions")
            execution_identities.add(identity)
        if remaining_executions:
            raise StudyRequestError("preflight omitted requested executions")
        expected_sources = {
            input_data.source
            for input_data in self.request.inputs_by_timeframe.values()
            if type(input_data) is ValidatedBarSet
        }
        if len(expected_sources) > 1:
            raise StudyRequestError("validated timeframe inputs must use one source identity")
        expected_source = next(iter(expected_sources), None)
        if self.source_identity != expected_source:
            raise StudyRequestError("preflight source identity does not match its inputs")
        expected_order = tuple(
            sorted(
                self.components,
                key=lambda component: (
                    SUPPORTED_TIMEFRAMES.index(component.timeframe),
                    component.execution.study_id,
                    component.execution.version,
                    canonical_inputs_json(component.prepared.normalized_config),
                ),
            )
        )
        if self.components != expected_order:
            raise StudyRequestError("preflight components are not in canonical order")
        canonical = _input_manifest_json(self.request, self.source_identity, self.components)
        expected_identity = _sha256_identity(INPUT_IDENTITY_VERSION, canonical)
        if self.input_manifest_json != canonical or self.input_snapshot_id != expected_identity:
            raise StudyRequestError("preflight input identity is not canonical")


class AnalysisCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    NO_AVAILABLE_COMPONENTS = "NO_AVAILABLE_COMPONENTS"


@dataclass(frozen=True, slots=True)
class ComponentResultEnvelope:
    """One final StudyResult paired with its validated preflight input identity."""

    component: PreflightComponent
    result: StudyResult

    def __post_init__(self) -> None:
        if type(self.component) is not PreflightComponent:
            raise StudyRequestError("result component must reference a preflight component")
        try:
            validate_study_result(self.result)
        except ValueError as error:
            raise StudyRequestError("component result is invalid") from error
        prepared = self.component.prepared
        if (
            self.result.study_id != prepared.study_id
            or self.result.version != prepared.version
            or self.result.instrument != prepared.request.instrument
            or self.result.timeframe != prepared.request.timeframe
            or self.result.as_of_utc != prepared.request.as_of_utc
            or self.result.inputs != dict(prepared.normalized_config)
        ):
            raise StudyRequestError("component result does not match its prepared request")
        expected_status = {
            PreflightEligibility.INSUFFICIENT_DATA: StudyStatus.INSUFFICIENT_DATA,
            PreflightEligibility.UNAVAILABLE: StudyStatus.UNAVAILABLE,
            PreflightEligibility.STALE: StudyStatus.STALE,
            PreflightEligibility.ERROR: StudyStatus.ERROR,
        }.get(self.component.eligibility)
        if expected_status is not None and self.result.status is not expected_status:
            raise StudyRequestError("component result status does not match preflight eligibility")
        if self.component.eligibility is PreflightEligibility.STALE:
            if (
                self.result.status is not StudyStatus.STALE
                or self.result.data_snapshot_id is not None
            ):
                raise StudyRequestError(
                    "stale preflight input requires a STALE result without snapshot"
                )
        elif self.result.data_snapshot_id != prepared.data_snapshot_id:
            raise StudyRequestError("component result snapshot does not match preflight input")
        elif self.result.status is StudyStatus.STALE:
            raise StudyRequestError("an eligible input cannot produce a STALE result")


@dataclass(frozen=True, slots=True)
class MultiTimeframeAnalysisResult:
    """Aggregate result contract; Stage 1 defines identity but does not execute studies."""

    preflight: MultiTimeframePreflight
    components: tuple[ComponentResultEnvelope, ...]
    completeness: AnalysisCompleteness = field(init=False)
    result_identity: str = field(init=False)
    result_manifest_json: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.preflight) is not MultiTimeframePreflight:
            raise StudyRequestError("analysis result requires a MultiTimeframePreflight")
        if type(self.components) is not tuple or len(self.components) != len(
            self.preflight.components
        ):
            raise StudyRequestError("analysis results must cover every preflight component")
        for expected, actual in zip(self.preflight.components, self.components, strict=True):
            if type(actual) is not ComponentResultEnvelope or actual.component != expected:
                raise StudyRequestError("analysis result components must match preflight ordering")

        available = sum(item.result.status is StudyStatus.AVAILABLE for item in self.components)
        if available == len(self.components):
            completeness = AnalysisCompleteness.COMPLETE
        elif available:
            completeness = AnalysisCompleteness.PARTIAL
        else:
            completeness = AnalysisCompleteness.NO_AVAILABLE_COMPONENTS
        manifest = _result_manifest_json(self.preflight, self.components)
        object.__setattr__(self, "completeness", completeness)
        object.__setattr__(self, "result_manifest_json", manifest)
        object.__setattr__(
            self,
            "result_identity",
            _sha256_identity(RESULT_IDENTITY_VERSION, manifest),
        )

    @property
    def input_snapshot_id(self) -> str:
        """Return the unchanged composite input identity used by the final result."""
        return self.preflight.input_snapshot_id


def preflight_multi_timeframe_analysis(
    request: MultiTimeframeAnalysisRequest, runner: StudyRunner
) -> MultiTimeframePreflight:
    """Validate all executions and inputs without cache lookup, computation or persistence."""
    if type(request) is not MultiTimeframeAnalysisRequest:
        raise StudyRequestError("request must be a MultiTimeframeAnalysisRequest")
    if not isinstance(runner, StudyRunner):
        raise StudyRequestError("runner must be a StudyRunner")

    requested_timeframes = {item.timeframe for item in request.executions}
    if set(request.inputs_by_timeframe) != requested_timeframes:
        raise StudyRequestError("input timeframe keys must exactly match requested timeframes")

    sources = {
        input_data.source
        for input_data in request.inputs_by_timeframe.values()
        if type(input_data) is ValidatedBarSet
    }
    if len(sources) > 1:
        raise StudyRequestError("validated timeframe inputs must use one source identity")
    source_identity = next(iter(sources), None)

    prepared_components: list[PreflightComponent] = []
    identities: set[tuple[str, str, str, str]] = set()
    for execution in request.executions:
        input_data = request.inputs_by_timeframe[execution.timeframe]
        run_request = StudyRunRequest(
            study_id=execution.study_id,
            version=execution.version,
            instrument=request.instrument,
            timeframe=execution.timeframe,
            as_of_utc=request.as_of_utc,
            config=execution.config,
        )
        prepared = runner.prepare(run_request, input_data)
        normalized_json = canonical_inputs_json(prepared.normalized_config)
        identity = (
            prepared.study_id,
            prepared.version,
            execution.timeframe,
            normalized_json,
        )
        if identity in identities:
            raise StudyRequestError("duplicate normalized study execution")
        identities.add(identity)

        max_age = request.freshness_policy.max_age_by_timeframe[execution.timeframe]
        if prepared.data_status is not DataStatus.VALID:
            eligibility = PreflightEligibility(prepared.data_status.value)
            bar_age = None
        else:
            assert prepared.latest_completed_at_utc is not None
            bar_age = request.as_of_utc - prepared.latest_completed_at_utc
            if bar_age < timedelta(0):
                raise StudyRequestError("latest completed input bar is after the analysis cutoff")
            if bar_age > max_age:
                eligibility = PreflightEligibility.STALE
            elif (
                prepared.input_bar_count is not None
                and prepared.input_bar_count < prepared.min_bars
            ):
                eligibility = PreflightEligibility.INSUFFICIENT_DATA
            else:
                eligibility = PreflightEligibility.ELIGIBLE
        prepared_components.append(
            PreflightComponent(execution, prepared, eligibility, max_age, bar_age)
        )

    components = tuple(
        sorted(
            prepared_components,
            key=lambda component: (
                SUPPORTED_TIMEFRAMES.index(component.timeframe),
                component.execution.study_id,
                component.execution.version,
                canonical_inputs_json(component.prepared.normalized_config),
            ),
        )
    )
    manifest = _input_manifest_json(request, source_identity, components)
    return MultiTimeframePreflight(
        request=request,
        source_identity=source_identity,
        components=components,
        input_manifest_json=manifest,
        input_snapshot_id=_sha256_identity(INPUT_IDENTITY_VERSION, manifest),
    )


def _input_manifest_json(
    request: MultiTimeframeAnalysisRequest,
    source_identity: str | None,
    components: tuple[PreflightComponent, ...],
) -> str:
    component_records: list[dict[str, object]] = []
    for component in components:
        prepared = component.prepared
        component_records.append(
            {
                "study_id": prepared.study_id,
                "version": prepared.version,
                "timeframe": component.timeframe,
                "normalized_inputs": dict(prepared.normalized_config),
                "data_status": prepared.data_status.value,
                "pre_execution_eligibility": component.eligibility.value,
                "failure_reasons": list(prepared.data_failure_reasons),
                "source_identity": prepared.input_source,
                "data_snapshot_id": prepared.data_snapshot_id,
                "input_bar_count": prepared.input_bar_count,
                "input_window_start_utc": (
                    None
                    if prepared.input_window_start_utc is None
                    else utc_z(prepared.input_window_start_utc)
                ),
                "input_window_end_utc": (
                    None
                    if prepared.input_window_end_utc is None
                    else utc_z(prepared.input_window_end_utc)
                ),
                "latest_completed_at_utc": (
                    None
                    if prepared.latest_completed_at_utc is None
                    else utc_z(prepared.latest_completed_at_utc)
                ),
                "bar_age_microseconds": (
                    None if component.bar_age is None else _duration_microseconds(component.bar_age)
                ),
                "max_age_microseconds": _duration_microseconds(component.max_age),
            }
        )
    return _canonical_json(
        {
            "algorithm": INPUT_IDENTITY_VERSION,
            "coordinator": {"id": COORDINATOR_ID, "version": COORDINATOR_VERSION},
            "instrument": request.instrument,
            "as_of_utc": utc_z(request.as_of_utc),
            "freshness_policy": {
                "id": request.freshness_policy.policy_id,
                "version": request.freshness_policy.version,
                "max_age_microseconds_by_timeframe": (
                    request.freshness_policy.canonical_configuration()
                ),
            },
            "source_identity": source_identity,
            "components": component_records,
        }
    )


def _result_manifest_json(
    preflight: MultiTimeframePreflight,
    components: tuple[ComponentResultEnvelope, ...],
) -> str:
    records: list[dict[str, object]] = []
    for envelope in components:
        result = envelope.result
        records.append(
            {
                "study_id": result.study_id,
                "version": result.version,
                "instrument": result.instrument,
                "timeframe": result.timeframe,
                "as_of_utc": utc_z(result.as_of_utc),
                "status": result.status.value,
                "state": result.state,
                "value": result.value,
                "normalized_value": result.normalized_value,
                "direction": result.direction.value,
                "uncertainty": result.uncertainty,
                "inputs": result.inputs,
                "evidence": list(result.evidence),
                "limitations": list(result.limitations),
                "data_snapshot_id": result.data_snapshot_id,
                "provenance": [list(pair) for pair in result.provenance],
            }
        )
    return _canonical_json(
        {
            "algorithm": RESULT_IDENTITY_VERSION,
            "input_snapshot_id": preflight.input_snapshot_id,
            "components": records,
        }
    )


def _sha256_identity(version: str, canonical: str) -> str:
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{version}:sha256:{digest}"


__all__ = [
    "AnalysisCompleteness",
    "ComponentResultEnvelope",
    "FreshnessPolicy",
    "MultiTimeframeAnalysisRequest",
    "MultiTimeframeAnalysisResult",
    "MultiTimeframePreflight",
    "PreflightComponent",
    "PreflightEligibility",
    "SUPPORTED_TIMEFRAMES",
    "StudyExecutionSpec",
    "TimeframeInput",
    "preflight_multi_timeframe_analysis",
]
