"""Instance-based registry for explicitly versioned bar studies."""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from inspect import signature
from types import MappingProxyType

from kankalar_gold_ai.core.contracts import MarketBar
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.studies.base import (
    BarStudy,
    ConfigValue,
    StudyOutput,
    validate_config_values,
)
from kankalar_gold_ai.studies.errors import (
    DuplicateStudyError,
    InvalidStudyError,
    StudyConfigError,
    UnknownStudyError,
)

_STUDY_ID_PATTERN = re.compile(
    r"^(?:trend|structure|volatility|regime|liquidity|macro)\."
    r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
)
_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SUPPORTED_TIMEFRAMES = frozenset(HistoricalDataPipeline.SUPPORTED_TIMEFRAMES)


def _version_key(version: str) -> tuple[tuple[int, str], tuple[int, str], tuple[int, str]]:
    match = _VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise InvalidStudyError(f"invalid study version: {version!r}")
    major, minor, patch = match.groups()
    return (len(major), major), (len(minor), minor), (len(patch), patch)


def _validate_method_signature(name: str, method: object, positional_count: int) -> None:
    """Ensure the bound method accepts exactly the call shape available to a runner."""
    if not callable(method):
        raise InvalidStudyError(f"study method {name!r} is not callable")
    try:
        method_signature = signature(method)
        method_signature.bind(*([object()] * positional_count))
    except Exception as error:
        raise InvalidStudyError(
            f"study method {name!r} cannot accept {positional_count} positional "
            f"argument(s) from the runner or its signature could not be inspected: {error}"
        ) from error


@dataclass(frozen=True, slots=True)
class RegisteredStudy:
    """A study reference paired with immutable metadata captured at registration.

    The study implementation remains caller-owned and is not made deeply immutable.
    The registry snapshots its ID, version and timeframe set. A future runner must
    use these entry fields as authoritative provenance instead of rereading the
    implementation object's mutable metadata.
    """

    study: BarStudy
    study_id: str
    version: str
    required_timeframes: frozenset[str]

    def validate_config(self, raw: Mapping[str, object]) -> Mapping[str, ConfigValue]:
        """Validate scalar inputs and return the study's normalized immutable config."""
        safe_raw = validate_config_values(raw)
        try:
            normalized = self.study.validate_config(MappingProxyType(safe_raw))
        except StudyConfigError:
            raise
        except Exception as error:
            raise StudyConfigError(f"{self.study_id} rejected its configuration") from error
        safe_normalized = validate_config_values(normalized)
        return MappingProxyType(safe_normalized)

    def min_bars(self, config: Mapping[str, object]) -> int:
        """Return a positive built-in integer warm-up count for normalized configuration."""
        safe_config = validate_config_values(config)
        try:
            count = self.study.min_bars(MappingProxyType(safe_config))
        except StudyConfigError:
            raise
        except Exception as error:
            raise StudyConfigError(f"{self.study_id} failed to determine min_bars") from error
        if type(count) is not int or count < 1:
            raise StudyConfigError(f"{self.study_id}: min_bars must return a positive integer")
        return count

    def compute(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, object],
    ) -> StudyOutput:
        """Invoke the study with a read-only copy of flat validated configuration."""
        safe_config = validate_config_values(config)
        return self.study.compute(bars, MappingProxyType(safe_config))


class StudyRegistry:
    """Deterministic registry keyed by the exact study ID and explicit version."""

    def __init__(self) -> None:
        self._studies: dict[tuple[str, str], RegisteredStudy] = {}

    def register(self, study: BarStudy) -> RegisteredStudy:
        """Validate and register a study, snapshotting all mutable metadata."""
        try:
            study_id: object = study.study_id
            version: object = study.version
            raw_timeframes: object = study.required_timeframes
        except AttributeError as error:
            raise InvalidStudyError("study is missing required metadata") from error

        if type(study_id) is not str:
            raise InvalidStudyError("study ID must be a built-in string")
        if not _STUDY_ID_PATTERN.fullmatch(study_id):
            raise InvalidStudyError(f"invalid study ID: {study_id!r}")
        if type(version) is not str:
            raise InvalidStudyError("study version must be a built-in string")
        if _VERSION_PATTERN.fullmatch(version) is None:
            raise InvalidStudyError(f"invalid study version: {version!r}")

        if isinstance(raw_timeframes, str):
            raise InvalidStudyError("required_timeframes must be a collection of timeframes")
        if not isinstance(raw_timeframes, Iterable):
            raise InvalidStudyError("required_timeframes must be iterable")
        validated_timeframes: list[str] = []
        try:
            for item in raw_timeframes:
                if type(item) is not str:
                    raise InvalidStudyError(
                        "required_timeframes must contain only built-in strings"
                    )
                validated_timeframes.append(item)
        except TypeError as error:
            raise InvalidStudyError("required_timeframes could not be iterated") from error
        timeframes = frozenset(validated_timeframes)
        if not timeframes:
            raise InvalidStudyError("required_timeframes must not be empty")
        unsupported = timeframes - _SUPPORTED_TIMEFRAMES
        if unsupported:
            raise InvalidStudyError(
                f"unsupported required timeframes: {', '.join(sorted(unsupported))}"
            )

        missing_methods: list[str] = []
        try:
            validate_config = study.validate_config
        except AttributeError:
            missing_methods.append("validate_config")
            validate_config = None
        try:
            min_bars = study.min_bars
        except AttributeError:
            missing_methods.append("min_bars")
            min_bars = None
        try:
            compute = study.compute
        except AttributeError:
            missing_methods.append("compute")
            compute = None
        missing_methods.extend(
            name
            for name, method in (
                ("validate_config", validate_config),
                ("min_bars", min_bars),
                ("compute", compute),
            )
            if method is not None and not callable(method)
        )
        if missing_methods:
            raise InvalidStudyError(
                f"study is missing required callable methods: {', '.join(missing_methods)}"
            )
        _validate_method_signature("validate_config", validate_config, 1)
        _validate_method_signature("min_bars", min_bars, 1)
        _validate_method_signature("compute", compute, 2)

        key = (study_id, version)
        if key in self._studies:
            raise DuplicateStudyError(f"study already registered: {study_id} {version}")
        registered = RegisteredStudy(study, study_id, version, timeframes)
        self._studies[key] = registered
        return registered

    def get(self, study_id: str, version: str) -> RegisteredStudy:
        """Return a study by exact ID and explicit version; never infer latest."""
        registered = self._studies.get((study_id, version))
        if registered is None:
            raise UnknownStudyError(f"unknown study: {study_id} {version}")
        return registered

    def list_studies(self) -> tuple[RegisteredStudy, ...]:
        """Return registrations sorted by ID and ascending numeric SemVer."""
        return tuple(
            sorted(
                self._studies.values(),
                key=lambda item: (item.study_id, _version_key(item.version)),
            )
        )

    def versions(self, study_id: str) -> tuple[str, ...]:
        """Return registered versions in ascending SemVer order."""
        versions = tuple(
            sorted(
                (version for registered_id, version in self._studies if registered_id == study_id),
                key=_version_key,
            )
        )
        if not versions:
            raise UnknownStudyError(f"unknown study ID: {study_id}")
        return versions
