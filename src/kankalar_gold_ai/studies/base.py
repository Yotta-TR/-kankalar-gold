"""Typed contracts shared by bar-based research studies."""

from __future__ import annotations

import math
from collections.abc import Mapping
from fractions import Fraction
from typing import NamedTuple, Protocol, TypeAlias, final

from kankalar_gold_ai.core.contracts import Direction, MarketBar, StudyStatus
from kankalar_gold_ai.studies.errors import StudyConfigError

ConfigValue: TypeAlias = str | int | float | bool | None

STUDY_OUTPUT_STATUSES = frozenset(
    {
        StudyStatus.AVAILABLE,
        StudyStatus.INSUFFICIENT_DATA,
        StudyStatus.UNAVAILABLE,
    }
)


class BarStudy(Protocol):
    """A deterministic computation over validated completed OHLC bars."""

    study_id: str
    version: str
    required_timeframes: frozenset[str]

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]: ...

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int: ...

    def compute(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, ConfigValue],
    ) -> StudyOutput: ...


def validate_config_values(values: Mapping[str, object]) -> dict[str, ConfigValue]:
    """Copy and validate the flat scalar configuration supported by StudyResult.inputs."""
    if not isinstance(values, Mapping):
        raise StudyConfigError("configuration must be a mapping")

    copied: dict[str, ConfigValue] = {}
    for key, value in values.items():
        if type(key) is not str or not key:
            raise StudyConfigError("configuration keys must be non-empty built-in strings")
        if value is None or type(value) is str or type(value) is bool or type(value) is int:
            copied[key] = value
        elif type(value) is float:
            if not math.isfinite(value):
                raise StudyConfigError(f"configuration value {key!r} must be finite")
            copied[key] = value
        else:
            raise StudyConfigError(f"configuration value {key!r} must be a built-in flat scalar")
    return copied


def _validated_output_number(name: str, value: object) -> float | None:
    if value is None:
        return None
    if type(value) is not int and type(value) is not float and type(value) is not Fraction:
        raise ValueError(f"{name} must be a finite number or None")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite number or None") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number or None")
    return number


def _string_tuple(name: str, values: object) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValueError(f"{name} must be an immutable tuple of strings")
    copied = tuple(values)
    if any(type(value) is not str for value in copied):
        raise ValueError(f"{name} must contain only built-in strings")
    return copied


class _StudyOutputFields(NamedTuple):
    status: StudyStatus
    state: str | None
    value: float | None
    normalized_value: float | None
    direction: Direction
    uncertainty: float | None
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]


@final
class StudyOutput(tuple[_StudyOutputFields]):
    """Immutable study output with status-specific value and direction rules.

    AVAILABLE permits any Direction and finite numeric values or None.
    INSUFFICIENT_DATA and UNAVAILABLE require NOT_APPLICABLE, no numeric values,
    and no state. Numeric inputs are normalized to immutable built-in floats.
    State, evidence and limitations contain only exact built-in strings.
    """

    __slots__ = ()

    def __init_subclass__(cls) -> None:
        raise TypeError("StudyOutput does not support subclassing")

    def __new__(
        cls,
        status: StudyStatus,
        state: str | None = None,
        value: float | None = None,
        normalized_value: float | None = None,
        direction: Direction = Direction.NOT_APPLICABLE,
        uncertainty: float | None = None,
        evidence: tuple[str, ...] = (),
        limitations: tuple[str, ...] = (),
    ) -> StudyOutput:
        if not isinstance(status, StudyStatus) or status not in STUDY_OUTPUT_STATUSES:
            raise ValueError(f"study output status is not allowed: {status!r}")
        if not isinstance(direction, Direction):
            raise ValueError("direction must be a Direction")
        if state is not None and type(state) is not str:
            raise ValueError("state must be a built-in string or None")

        if status in {StudyStatus.INSUFFICIENT_DATA, StudyStatus.UNAVAILABLE}:
            if direction is not Direction.NOT_APPLICABLE:
                raise ValueError(f"{status} requires NOT_APPLICABLE direction")
            if any(item is not None for item in (value, normalized_value, uncertainty)):
                raise ValueError(f"{status} requires all numeric fields to be None")
            if state is not None:
                raise ValueError(f"{status} requires state to be None")

        fields = _StudyOutputFields(
            status,
            state,
            _validated_output_number("value", value),
            _validated_output_number("normalized_value", normalized_value),
            direction,
            _validated_output_number("uncertainty", uncertainty),
            _string_tuple("evidence", evidence),
            _string_tuple("limitations", limitations),
        )
        return tuple.__new__(cls, (fields,))

    @property
    def status(self) -> StudyStatus:
        return self[0].status

    @property
    def state(self) -> str | None:
        return self[0].state

    @property
    def value(self) -> float | None:
        return self[0].value

    @property
    def normalized_value(self) -> float | None:
        return self[0].normalized_value

    @property
    def direction(self) -> Direction:
        return self[0].direction

    @property
    def uncertainty(self) -> float | None:
        return self[0].uncertainty

    @property
    def evidence(self) -> tuple[str, ...]:
        return self[0].evidence

    @property
    def limitations(self) -> tuple[str, ...]:
        return self[0].limitations

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return False
        return self[0] == other[0]

    def __ne__(self, other: object) -> bool:
        return not self == other

    def __hash__(self) -> int:
        return hash(self[0])

    def __reduce__(self) -> tuple[type[StudyOutput], tuple[object, ...]]:
        return StudyOutput, tuple(self[0])

    def __repr__(self) -> str:
        return (
            f"StudyOutput(status={self.status!r}, state={self.state!r}, "
            f"value={self.value!r}, normalized_value={self.normalized_value!r}, "
            f"direction={self.direction!r}, uncertainty={self.uncertainty!r}, "
            f"evidence={self.evidence!r}, limitations={self.limitations!r})"
        )
