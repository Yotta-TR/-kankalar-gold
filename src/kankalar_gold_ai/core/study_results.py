"""Dependency-light StudyResult validation and cache identity.

This module imports only ``core.contracts`` so both the study runner and SQLite
deserialization can share one validator without importing each other's layers.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kankalar_gold_ai.core.contracts import Direction, StudyResult, StudyStatus

CACHE_FORMAT_VERSION = "study-result-v1"

CACHEABLE_STATUSES = frozenset(
    {StudyStatus.AVAILABLE, StudyStatus.INSUFFICIENT_DATA, StudyStatus.UNAVAILABLE}
)

PROV_CACHE_FORMAT = "cache_format_version"
PROV_STUDY_VERSION = "study_version"
PROV_WINDOW_START = "input_window_start_utc"
PROV_WINDOW_END = "input_window_end_utc"
PROV_BAR_COUNT = "input_bar_count"
PROV_SOURCE = "input_source"
PROV_DATA_CHECKED = "data_checked_at_utc"

_WINDOW_KEYS = frozenset({PROV_WINDOW_START, PROV_WINDOW_END, PROV_BAR_COUNT, PROV_SOURCE})
_ALLOWED_PROVENANCE_KEYS = (
    frozenset({PROV_CACHE_FORMAT, PROV_STUDY_VERSION, PROV_DATA_CHECKED}) | _WINDOW_KEYS
)

_UTC_Z = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?Z")
_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*")


class StudyResultValidationError(ValueError):
    """A StudyResult or cache key violates the persisted-result contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StudyResultValidationError(message)


def utc_z(value: datetime) -> str:
    """Serialize an aware timestamp as canonical UTC ISO-8601 with a trailing Z."""
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise StudyResultValidationError("timestamp must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc_z(text: object) -> datetime:
    """Parse only the canonical form produced by ``utc_z``."""
    if type(text) is not str or _UTC_Z.fullmatch(text) is None:
        raise StudyResultValidationError("timestamp is not canonical UTC ISO-8601")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise StudyResultValidationError("timestamp is not a valid datetime") from error
    if utc_z(parsed) != text:
        raise StudyResultValidationError("timestamp is not canonical UTC ISO-8601")
    return parsed


def canonical_inputs_json(inputs: Mapping[str, object]) -> str:
    """Canonical text for normalized configuration; matches the SQLite save path."""
    return json.dumps(dict(inputs), sort_keys=True)


@dataclass(frozen=True, slots=True)
class StudyResultKey:
    """Cache identity: every component participates in the SQLite lookup."""

    study_id: str
    version: str
    instrument: str
    timeframe: str
    as_of_utc: datetime
    inputs_json: str
    data_snapshot_id: str
    cache_format_version: str = CACHE_FORMAT_VERSION

    def __post_init__(self) -> None:
        for name, text in (
            ("study_id", self.study_id),
            ("version", self.version),
            ("instrument", self.instrument),
            ("timeframe", self.timeframe),
            ("inputs_json", self.inputs_json),
        ):
            _require(type(text) is str, f"{name} must be a built-in string")
        _require(
            type(self.data_snapshot_id) is str and bool(self.data_snapshot_id),
            "data_snapshot_id must be a non-empty built-in string",
        )
        _require(
            self.cache_format_version == CACHE_FORMAT_VERSION,
            "unsupported cache format version",
        )
        object.__setattr__(self, "as_of_utc", parse_utc_z(utc_z(self.as_of_utc)))


def provenance_mapping(provenance: object) -> dict[str, str]:
    """Validate provenance shape: sorted tuple of unique built-in string pairs."""
    _require(type(provenance) is tuple, "provenance must be a tuple")
    assert isinstance(provenance, tuple)
    mapping: dict[str, str] = {}
    for item in provenance:
        _require(
            type(item) is tuple
            and len(item) == 2
            and type(item[0]) is str
            and type(item[1]) is str,
            "provenance entries must be pairs of built-in strings",
        )
        key, value = item
        _require(key not in mapping, f"duplicate provenance key: {key}")
        mapping[key] = value
    _require(list(mapping) == sorted(mapping), "provenance must be sorted by key")
    return mapping


def _validate_provenance(result: StudyResult) -> None:
    mapping = provenance_mapping(result.provenance)
    unknown = set(mapping) - _ALLOWED_PROVENANCE_KEYS
    _require(not unknown, f"unknown provenance keys: {sorted(unknown)}")
    _require(
        mapping.get(PROV_CACHE_FORMAT) == CACHE_FORMAT_VERSION,
        "provenance cache_format_version is missing or unsupported",
    )
    _require(
        mapping.get(PROV_STUDY_VERSION) == result.version,
        "provenance study_version must equal the result version",
    )
    present_window = _WINDOW_KEYS & set(mapping)
    if result.data_snapshot_id is not None:
        _require(
            present_window == _WINDOW_KEYS, "snapshot results require full input-window provenance"
        )
        _require(
            PROV_DATA_CHECKED not in mapping,
            "snapshot results must not carry data_checked_at_utc",
        )
        start = parse_utc_z(mapping[PROV_WINDOW_START])
        end = parse_utc_z(mapping[PROV_WINDOW_END])
        _require(start < end, "input window start must precede its end")
        _require(end <= result.as_of_utc, "input window must not extend past as_of_utc")
        _require(
            _POSITIVE_INTEGER.fullmatch(mapping[PROV_BAR_COUNT]) is not None,
            "input_bar_count must be a positive integer",
        )
        _require(bool(mapping[PROV_SOURCE]), "input_source must be non-empty")
    else:
        _require(not present_window, "results without a snapshot must not carry window fields")
        _require(
            PROV_DATA_CHECKED in mapping,
            "results without a snapshot require data_checked_at_utc",
        )
        checked = parse_utc_z(mapping[PROV_DATA_CHECKED])
        _require(checked >= result.as_of_utc, "data check must not precede as_of_utc")


def _is_float_or_none(value: object) -> bool:
    return value is None or (type(value) is float and math.isfinite(value))


def validate_study_result(result: StudyResult) -> None:
    """Enforce the StudyResult status matrix, flat inputs and provenance semantics."""
    _require(type(result) is StudyResult, "result must be a StudyResult")
    for name, text in (
        ("study_id", result.study_id),
        ("version", result.version),
        ("instrument", result.instrument),
        ("timeframe", result.timeframe),
    ):
        _require(type(text) is str and bool(text), f"{name} must be a non-empty built-in string")
    _require(
        type(result.as_of_utc) is datetime and result.as_of_utc.utcoffset() == timedelta(0),
        "as_of_utc must be a UTC datetime",
    )
    _require(type(result.status) is StudyStatus, "status must be a StudyStatus")
    _require(type(result.direction) is Direction, "direction must be a Direction")
    _require(result.state is None or type(result.state) is str, "state must be a string or None")
    for name, number in (
        ("value", result.value),
        ("normalized_value", result.normalized_value),
        ("uncertainty", result.uncertainty),
    ):
        _require(_is_float_or_none(number), f"{name} must be finite or None")
    snapshot = result.data_snapshot_id
    _require(
        snapshot is None or (type(snapshot) is str and bool(snapshot)),
        "data_snapshot_id must be a non-empty string or None",
    )

    _require(type(result.inputs) is dict, "inputs must be a dict")
    for key, value in result.inputs.items():
        _require(type(key) is str and bool(key), "input keys must be non-empty strings")
        _require(
            value is None
            or type(value) in (str, bool, int)
            or (type(value) is float and math.isfinite(value)),
            "input values must be finite flat scalars",
        )
    for name, items in (("evidence", result.evidence), ("limitations", result.limitations)):
        _require(
            type(items) is tuple and all(type(item) is str for item in items),
            f"{name} must be a tuple of built-in strings",
        )

    if result.status is StudyStatus.AVAILABLE:
        _require(snapshot is not None, "AVAILABLE results require a data snapshot")
    else:
        _require(
            result.state is None
            and result.value is None
            and result.normalized_value is None
            and result.uncertainty is None
            and result.direction is Direction.NOT_APPLICABLE,
            f"{result.status} requires null values and NOT_APPLICABLE direction",
        )
    _validate_provenance(result)


def is_cache_eligible(result: StudyResult) -> bool:
    """Only deterministic outcomes with a snapshot and current format may be served."""
    return (
        result.status in CACHEABLE_STATUSES
        and result.data_snapshot_id is not None
        and dict(result.provenance).get(PROV_CACHE_FORMAT) == CACHE_FORMAT_VERSION
    )
