"""Study interface and registry tests use fake studies only."""

import copy
import pickle
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from fractions import Fraction
from types import MappingProxyType

import pytest

from kankalar_gold_ai.core.contracts import Direction, MarketBar, StudyStatus
from kankalar_gold_ai.studies.base import ConfigValue, StudyOutput, validate_config_values
from kankalar_gold_ai.studies.errors import (
    DuplicateStudyError,
    InvalidStudyError,
    StudyConfigError,
    UnknownStudyError,
)
from kankalar_gold_ai.studies.registry import RegisteredStudy, StudyRegistry


class FakeStudy:
    def __init__(
        self,
        study_id: str = "trend.example_study",
        version: str = "1.0.0",
        required_timeframes: object = frozenset({"M1"}),
        min_bars_value: object = 1,
    ) -> None:
        self.study_id = study_id
        self.version = version
        self.required_timeframes = required_timeframes
        self.min_bars_value = min_bars_value
        self.config_calls = 0

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        self.config_calls += 1
        return raw

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        return self.min_bars_value  # type: ignore[return-value]

    def compute(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, ConfigValue],
    ) -> StudyOutput:
        return StudyOutput(status=StudyStatus.AVAILABLE, value=None)


class MutableHashString(str):
    offset = 0

    def __hash__(self) -> int:
        return super().__hash__() + self.offset


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("study_id", "trend.example_study", "study ID"),
        ("version", "1.0.0", "version"),
        ("required_timeframes", "M1", "required_timeframes"),
    ),
)
def test_registry_rejects_mutable_string_metadata_without_inserting(
    field: str, value: str, message: str
) -> None:
    mutable_value = MutableHashString(value)
    study = FakeStudy(
        study_id=mutable_value if field == "study_id" else "trend.example_study",
        version=mutable_value if field == "version" else "1.0.0",
        required_timeframes=[mutable_value] if field == "required_timeframes" else {"M1"},
    )
    registry = StudyRegistry()

    with pytest.raises(InvalidStudyError, match=message):
        registry.register(study)
    mutable_value.offset = 1
    with pytest.raises(InvalidStudyError, match=message):
        registry.register(study)
    assert registry.list_studies() == ()


def test_registry_rejects_mutable_id_before_it_can_corrupt_duplicate_detection() -> None:
    mutable_id = MutableHashString("trend.example_study")
    study = FakeStudy(study_id=mutable_id)
    registry = StudyRegistry()
    with pytest.raises(InvalidStudyError, match="study ID"):
        registry.register(study)
    mutable_id.offset = 1
    with pytest.raises(InvalidStudyError, match="study ID"):
        registry.register(study)
    assert registry.list_studies() == ()

    registered = registry.register(FakeStudy())
    with pytest.raises(DuplicateStudyError):
        registry.register(FakeStudy())
    assert registry.list_studies() == (registered,)
    assert registry.get("trend.example_study", "1.0.0") is registered


@pytest.mark.parametrize("field", ("study_id", "version", "required_timeframes"))
def test_registry_rejects_string_subclasses_without_invoking_custom_methods(field: str) -> None:
    class DangerousString(str):
        def __hash__(self) -> int:
            raise AssertionError("custom hash called")

        def __eq__(self, other: object) -> bool:
            raise AssertionError("custom equality called")

        def __str__(self) -> str:
            raise AssertionError("custom conversion called")

        def __repr__(self) -> str:
            raise AssertionError("custom representation called")

    study = FakeStudy(
        study_id=DangerousString("trend.example_study")
        if field == "study_id"
        else "trend.example_study",
        version=DangerousString("1.0.0") if field == "version" else "1.0.0",
        required_timeframes=["M1", DangerousString("M1")]
        if field == "required_timeframes"
        else {"M1"},
    )
    registry = StudyRegistry()
    with pytest.raises(InvalidStudyError):
        registry.register(study)
    assert registry.list_studies() == ()


def test_registry_metadata_snapshot_contains_only_builtin_strings() -> None:
    study = FakeStudy(required_timeframes={"M1", "H1"})
    registry = StudyRegistry()
    entry = registry.register(study)
    study.study_id = "macro.changed"
    study.version = "2.0.0"
    study.required_timeframes = {"D1"}

    assert type(entry.study_id) is str
    assert type(entry.version) is str
    assert type(entry.required_timeframes) is frozenset
    assert all(type(timeframe) is str for timeframe in entry.required_timeframes)
    assert entry.required_timeframes == frozenset({"M1", "H1"})
    assert registry.get("trend.example_study", "1.0.0") is entry
    with pytest.raises(DuplicateStudyError):
        registry.register(FakeStudy())


def test_register_and_lookup_by_explicit_id_and_version() -> None:
    registry = StudyRegistry()
    study = FakeStudy()

    registered = registry.register(study)

    assert isinstance(registered, RegisteredStudy)
    assert registry.get("trend.example_study", "1.0.0") is registered
    assert registered.study is study
    assert registered.study_id == "trend.example_study"
    assert registered.version == "1.0.0"
    assert registered.required_timeframes == frozenset({"M1"})


def test_duplicate_registration_rejected_but_explicit_versions_allowed() -> None:
    registry = StudyRegistry()
    registry.register(FakeStudy(version="1.0.0"))

    with pytest.raises(DuplicateStudyError):
        registry.register(FakeStudy(version="1.0.0"))

    second = registry.register(FakeStudy(version="2.0.0"))
    assert second.version == "2.0.0"
    assert registry.versions("trend.example_study") == ("1.0.0", "2.0.0")


def test_versions_are_strict_semver_and_sorted_numerically() -> None:
    registry = StudyRegistry()
    for version in ("1.9.0", "1.10.0", "2.0.0"):
        registry.register(FakeStudy(version=version))

    assert registry.versions("trend.example_study") == ("1.9.0", "1.10.0", "2.0.0")


@pytest.mark.parametrize("component_index", (0, 1, 2))
def test_versions_with_extremely_long_components_sort_numerically(component_index: int) -> None:
    components = (
        "9" * 4300,
        "1" + "0" * 4300,
        "2" + "0" * 4300,
        "9" * 4301,
        "1" + "0" * 4301,
    )
    versions = []
    for component in components:
        parts = ["1", "1", "1"]
        parts[component_index] = component
        versions.append(".".join(parts))

    registry = StudyRegistry()
    for version in reversed(versions):
        registry.register(FakeStudy(version=version))

    expected = tuple(versions)
    for _ in range(2):
        assert registry.versions("trend.example_study") == expected
        assert tuple(entry.version for entry in registry.list_studies()) == expected


def test_long_version_metadata_keeps_snapshot_and_exact_string_requirements() -> None:
    version = "9" * 4301 + ".0.0"
    study = FakeStudy(version=version, required_timeframes={"M1"})
    registry = StudyRegistry()
    registered = registry.register(study)
    study.version = "1.0.0"
    study.study_id = "macro.changed"
    study.required_timeframes = {"H1"}

    assert type(registered.version) is str
    assert registered.version == version
    assert registered.required_timeframes == frozenset({"M1"})
    assert registry.get("trend.example_study", version) is registered
    with pytest.raises(DuplicateStudyError):
        registry.register(FakeStudy(version=version))
    with pytest.raises(InvalidStudyError, match="version"):
        registry.register(FakeStudy(version=MutableHashString(version)))
    assert registry.versions("trend.example_study") == (version,)
    assert registry.list_studies() == (registered,)


def test_unknown_study_and_version_raise() -> None:
    registry = StudyRegistry()
    registry.register(FakeStudy())

    with pytest.raises(UnknownStudyError):
        registry.get("trend.unknown", "1.0.0")
    with pytest.raises(UnknownStudyError):
        registry.get("trend.example_study", "9.0.0")
    with pytest.raises(UnknownStudyError):
        registry.versions("trend.unknown")


@pytest.mark.parametrize(
    ("study", "message"),
    (
        (FakeStudy(study_id="other.example"), "invalid study ID"),
        (FakeStudy(study_id="trend.Bad_name"), "invalid study ID"),
        (FakeStudy(version="1.0"), "invalid study version"),
        (FakeStudy(version="01.0.0"), "invalid study version"),
        (FakeStudy(version="1.00.0"), "invalid study version"),
        (FakeStudy(version="0.1.01"), "invalid study version"),
        (FakeStudy(version="1.0.0-alpha"), "invalid study version"),
        (FakeStudy(version="1.0.0+build"), "invalid study version"),
        (FakeStudy(required_timeframes=frozenset()), "must not be empty"),
        (FakeStudy(required_timeframes=frozenset({"W1"})), "unsupported"),
    ),
)
def test_invalid_metadata_is_rejected(study: FakeStudy, message: str) -> None:
    with pytest.raises(InvalidStudyError, match=message):
        StudyRegistry().register(study)


def test_missing_required_method_is_rejected() -> None:
    class MissingComputeStudy:
        study_id = "trend.example_study"
        version = "1.0.0"
        required_timeframes = frozenset({"M1"})

        def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
            return raw

        def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
            return 1

    with pytest.raises(InvalidStudyError, match="compute"):
        StudyRegistry().register(MissingComputeStudy())  # type: ignore[arg-type]


def test_registration_rejects_method_with_too_few_parameters() -> None:
    class TooFewParametersStudy(FakeStudy):
        def compute(self) -> StudyOutput:  # type: ignore[override]
            return StudyOutput(status=StudyStatus.AVAILABLE)

    with pytest.raises(InvalidStudyError, match="compute.*2 positional"):
        StudyRegistry().register(TooFewParametersStudy())


def test_registration_rejects_required_extra_parameter() -> None:
    class ExtraRequiredParameterStudy(FakeStudy):
        def min_bars(self, config: Mapping[str, ConfigValue], required: int) -> int:
            return required

    with pytest.raises(InvalidStudyError, match="min_bars.*1 positional"):
        StudyRegistry().register(ExtraRequiredParameterStudy())


def test_registration_rejects_keyword_only_required_parameter() -> None:
    class KeywordOnlyStudy(FakeStudy):
        def validate_config(self, *, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
            return raw

    with pytest.raises(InvalidStudyError, match="validate_config.*1 positional"):
        StudyRegistry().register(KeywordOnlyStudy())


def test_registration_rejects_non_callable_method_attribute() -> None:
    study = FakeStudy()
    study.compute = 42  # type: ignore[assignment]

    with pytest.raises(InvalidStudyError, match="compute"):
        StudyRegistry().register(study)


def test_registration_handles_uninspectable_callable_signature() -> None:
    class UninspectableCallable:
        @property
        def __signature__(self) -> object:
            raise ValueError("signature metadata unavailable")

        def __call__(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
            return raw

    study = FakeStudy()
    study.validate_config = UninspectableCallable()  # type: ignore[assignment]

    with pytest.raises(InvalidStudyError, match="validate_config.*cannot accept"):
        StudyRegistry().register(study)


def test_registration_wraps_unexpected_signature_inspection_error() -> None:
    class UninspectableCallable:
        @property
        def __signature__(self) -> object:
            raise RuntimeError("signature inspection failed")

        def __call__(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
            return raw

    study = FakeStudy()
    study.validate_config = UninspectableCallable()  # type: ignore[assignment]

    with pytest.raises(
        InvalidStudyError, match="validate_config.*signature inspection failed"
    ) as e:
        StudyRegistry().register(study)
    assert isinstance(e.value.__cause__, RuntimeError)


@pytest.mark.parametrize("exception", (KeyboardInterrupt, SystemExit))
def test_registration_does_not_swallow_process_control_exceptions(
    exception: type[BaseException],
) -> None:
    class UninspectableCallable:
        @property
        def __signature__(self) -> object:
            raise exception()

        def __call__(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
            return raw

    study = FakeStudy()
    study.validate_config = UninspectableCallable()  # type: ignore[assignment]
    with pytest.raises(exception):
        StudyRegistry().register(study)


def test_registration_accepts_optional_extra_parameters_without_running_compute() -> None:
    class OptionalExtraParametersStudy(FakeStudy):
        compute_calls = 0

        def validate_config(
            self, raw: Mapping[str, ConfigValue], strict: bool = True
        ) -> Mapping[str, ConfigValue]:
            return raw

        def min_bars(self, config: Mapping[str, ConfigValue], default: int = 1) -> int:
            return default

        def compute(
            self,
            bars: tuple[MarketBar, ...],
            config: Mapping[str, ConfigValue],
            detailed: bool = False,
        ) -> StudyOutput:
            self.compute_calls += 1
            return StudyOutput(status=StudyStatus.AVAILABLE)

    study = OptionalExtraParametersStudy()
    StudyRegistry().register(study)

    assert study.compute_calls == 0


def test_registry_snapshots_mutable_timeframe_metadata() -> None:
    timeframes = {"M1"}
    study = FakeStudy(required_timeframes=timeframes)
    registry = StudyRegistry()
    registered = registry.register(study)
    timeframes.add("H1")

    assert registered.required_timeframes == frozenset({"M1"})
    assert registry.get("trend.example_study", "1.0.0").required_timeframes == frozenset({"M1"})


def test_registry_snapshot_survives_study_metadata_reassignment() -> None:
    registry = StudyRegistry()
    study = FakeStudy()
    registered = registry.register(study)

    study.study_id = "macro.reassigned"
    study.version = "9.0.0"
    study.required_timeframes = {"H1"}

    assert registry.get("trend.example_study", "1.0.0") is registered
    assert registry.list_studies() == (registered,)
    assert registered.study_id == "trend.example_study"
    assert registered.version == "1.0.0"
    assert registered.required_timeframes == frozenset({"M1"})
    with pytest.raises(UnknownStudyError):
        registry.get("macro.reassigned", "9.0.0")


def test_registered_study_metadata_cannot_be_reassigned() -> None:
    registered = StudyRegistry().register(FakeStudy())

    with pytest.raises(FrozenInstanceError):
        registered.study_id = "macro.changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        registered.version = "2.0.0"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        registered.required_timeframes = frozenset({"H1"})  # type: ignore[misc]


def test_sorted_listing_and_registry_instances_are_independent() -> None:
    first = StudyRegistry()
    second = StudyRegistry()
    first.register(FakeStudy("trend.z_study", "2.0.0"))
    first.register(FakeStudy("trend.a_study", "1.0.0"))
    first.register(FakeStudy("trend.z_study", "1.0.0"))

    assert [(item.study_id, item.version) for item in first.list_studies()] == [
        ("trend.a_study", "1.0.0"),
        ("trend.z_study", "1.0.0"),
        ("trend.z_study", "2.0.0"),
    ]
    assert second.list_studies() == ()


@pytest.mark.parametrize(
    "config",
    (
        {"nested": {"value": 1}},
        {"object": object()},
        {"non_finite": float("nan")},
        {"": "empty-key"},
    ),
)
def test_invalid_configuration_values_raise_typed_error(config: dict[str, object]) -> None:
    registered = StudyRegistry().register(FakeStudy())

    with pytest.raises(StudyConfigError):
        registered.validate_config(config)


def test_valid_configuration_is_copied_and_normalized_to_read_only_mapping() -> None:
    study = FakeStudy()
    registered = StudyRegistry().register(study)
    raw: dict[str, object] = {"enabled": True, "limit": 4, "name": "sample"}

    normalized = registered.validate_config(raw)
    raw["limit"] = 20

    assert normalized == {"enabled": True, "limit": 4, "name": "sample"}
    with pytest.raises(TypeError):
        normalized["limit"] = 5  # type: ignore[index]
    assert normalized["enabled"] is True
    assert type(normalized["enabled"]) is bool


class HostileConfigString(str):
    def __str__(self) -> str:
        raise AssertionError("string conversion called")


class HostileConfigInt(int):
    def __lt__(self, other: object) -> bool:
        raise AssertionError("comparison called")

    def __int__(self) -> int:
        raise AssertionError("integer conversion called")


class HostileConfigFloat(float):
    def __float__(self) -> float:
        raise AssertionError("float conversion called")


class SpoofingMeta(type):
    equality_calls = 0

    def __eq__(cls, other: object) -> bool:
        SpoofingMeta.equality_calls += 1
        return True

    def __getattribute__(cls, name: str) -> object:
        if name == "__name__":
            raise AssertionError("type name accessed")
        return super().__getattribute__(name)


class SpoofedScalar(metaclass=SpoofingMeta):
    def __init__(self) -> None:
        self.payload: list[object] = []

    @property
    def __class__(self) -> type:
        raise AssertionError("spoofed class accessed")

    def __repr__(self) -> str:
        raise AssertionError("representation called")

    def __float__(self) -> float:
        raise AssertionError("float conversion called")

    def __index__(self) -> int:
        raise AssertionError("index conversion called")


class SpoofedStringScalar(SpoofedScalar):
    @property
    def __class__(self) -> type:
        return str


class SpoofedNumericScalar(SpoofedScalar):
    @property
    def __class__(self) -> type:
        return float


class HostileKey(str):
    def __hash__(self) -> int:
        raise AssertionError("hash called")

    def __eq__(self, other: object) -> bool:
        raise AssertionError("equality called")

    def __repr__(self) -> str:
        raise AssertionError("representation called")


class SingleItemMapping(Mapping[object, object]):
    def __init__(self, key: object, value: object) -> None:
        self.key = key
        self.value = value

    def __getitem__(self, key: object) -> object:
        raise AssertionError("lookup called")

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter((self.key,))

    def __len__(self) -> int:
        return 1

    def items(self):  # type: ignore[no-untyped-def]
        return iter(((self.key, self.value),))


def test_hostile_config_key_is_rejected_without_using_its_methods() -> None:
    for key in ("", MutableHashString("key"), HostileKey("key"), SpoofedStringScalar()):
        with pytest.raises(StudyConfigError, match="keys"):
            validate_config_values(SingleItemMapping(key, 1))  # type: ignore[arg-type]


def test_spoofed_config_values_are_rejected_without_using_untrusted_methods() -> None:
    SpoofingMeta.equality_calls = 0
    for value in (SpoofedScalar(), SpoofedStringScalar(), SpoofedNumericScalar()):
        with pytest.raises(StudyConfigError, match="'value'"):
            validate_config_values({"value": value})
    assert SpoofingMeta.equality_calls == 0


def test_spoofed_config_values_never_reach_registered_study_methods() -> None:
    class CountingStudy(FakeStudy):
        def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
            raise AssertionError("min_bars called")

        def compute(
            self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
        ) -> StudyOutput:
            raise AssertionError("compute called")

    SpoofingMeta.equality_calls = 0
    study = CountingStudy()
    registered = StudyRegistry().register(study)
    for value in (SpoofedScalar(), SpoofedStringScalar(), SpoofedNumericScalar()):
        config = {"value": value}
        with pytest.raises(StudyConfigError, match="'value'"):
            registered.validate_config(config)
        with pytest.raises(StudyConfigError, match="'value'"):
            registered.min_bars(config)
        with pytest.raises(StudyConfigError, match="'value'"):
            registered.compute((), config)
    assert study.config_calls == 0
    assert SpoofingMeta.equality_calls == 0


def test_spoofed_normalized_config_value_is_rejected() -> None:
    class NormalizingStudy(FakeStudy):
        def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
            return {"value": SpoofedStringScalar()}  # type: ignore[dict-item]

    SpoofingMeta.equality_calls = 0
    with pytest.raises(StudyConfigError, match="'value'"):
        StudyRegistry().register(NormalizingStudy()).validate_config({})
    assert SpoofingMeta.equality_calls == 0


@pytest.mark.parametrize("value", ([], (), {}, object()))
def test_config_rejects_unsupported_containers_and_objects(value: object) -> None:
    with pytest.raises(StudyConfigError, match="'value'"):
        validate_config_values({"value": value})


@pytest.mark.parametrize(
    ("config", "message"),
    (
        ({MutableHashString("key"): "value"}, "keys"),
        ({"value": HostileConfigString("value")}, "'value'"),
        ({"value": HostileConfigInt(0)}, "'value'"),
        ({"value": HostileConfigFloat(1.0)}, "'value'"),
        ({"value": float("nan")}, "finite"),
        ({"value": float("inf")}, "finite"),
        ({"value": float("-inf")}, "finite"),
    ),
)
def test_invalid_config_rejected_before_any_study_method(
    config: dict[str, object], message: str
) -> None:
    class CountingStudy(FakeStudy):
        def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
            raise AssertionError("min_bars called")

        def compute(
            self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
        ) -> StudyOutput:
            raise AssertionError("compute called")

    study = CountingStudy()
    registered = StudyRegistry().register(study)
    with pytest.raises(StudyConfigError, match=message):
        validate_config_values(config)
    with pytest.raises(StudyConfigError, match=message):
        registered.validate_config(config)
    with pytest.raises(StudyConfigError, match=message):
        registered.min_bars(config)
    with pytest.raises(StudyConfigError, match=message):
        registered.compute((), config)
    assert study.config_calls == 0


@pytest.mark.parametrize(
    ("normalized", "message"),
    (
        ({MutableHashString("key"): 1}, "keys"),
        ({"value": HostileConfigString("value")}, "'value'"),
        ({"value": HostileConfigInt(0)}, "'value'"),
        ({"value": HostileConfigFloat(1.0)}, "'value'"),
        ({"value": float("nan")}, "finite"),
        ({"value": float("inf")}, "finite"),
        ({"value": float("-inf")}, "finite"),
    ),
)
def test_invalid_study_normalized_configuration_is_rejected(
    normalized: dict[str, ConfigValue], message: str
) -> None:
    class NormalizingStudy(FakeStudy):
        def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
            return normalized

    registered = StudyRegistry().register(NormalizingStudy())
    with pytest.raises(StudyConfigError, match=message):
        registered.validate_config({})


def test_builtin_config_values_and_read_only_mappings_are_copied() -> None:
    raw: dict[str, ConfigValue] = {
        "text": "value",
        "integer": 3,
        "float": 1.5,
        "enabled": True,
        "disabled": False,
        "optional": None,
    }
    read_only = MappingProxyType(raw)
    copied = validate_config_values(read_only)
    registered = StudyRegistry().register(FakeStudy())
    normalized = registered.validate_config(read_only)
    raw["integer"] = 4
    expected = {
        "text": "value",
        "integer": 3,
        "float": 1.5,
        "enabled": True,
        "disabled": False,
        "optional": None,
    }
    assert copied == normalized == expected
    assert type(normalized["enabled"]) is bool
    assert type(normalized["disabled"]) is bool
    with pytest.raises(TypeError):
        normalized["integer"] = 5  # type: ignore[index]


def test_study_normalized_configuration_is_copied() -> None:
    returned: dict[str, ConfigValue] = {"limit": 1}

    class NormalizingStudy(FakeStudy):
        def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
            return returned

    normalized = StudyRegistry().register(NormalizingStudy()).validate_config({})
    returned["limit"] = 2
    assert normalized["limit"] == 1
    with pytest.raises(TypeError):
        normalized["limit"] = 3  # type: ignore[index]


def test_study_methods_receive_independent_read_only_configurations() -> None:
    class CapturingStudy(FakeStudy):
        def __init__(self) -> None:
            super().__init__()
            self.received: list[Mapping[str, ConfigValue]] = []

        def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
            self.received.append(raw)
            return raw

        def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
            self.received.append(config)
            return 1

        def compute(
            self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
        ) -> StudyOutput:
            self.received.append(config)
            return StudyOutput(StudyStatus.AVAILABLE)

    study = CapturingStudy()
    registered = StudyRegistry().register(study)
    raw: dict[str, ConfigValue] = {"value": 1}
    normalized = registered.validate_config(MappingProxyType(raw))
    assert registered.min_bars(raw) == 1
    registered.compute((), raw)
    raw["value"] = 2
    assert normalized["value"] == 1
    assert len(study.received) == 3
    for config in study.received:
        assert type(config) is MappingProxyType
        assert config["value"] == 1
        with pytest.raises(TypeError):
            config["value"] = 3  # type: ignore[index]


@pytest.mark.parametrize("min_bars", (True, 0, -1, 1.5, "2", None))
def test_invalid_min_bars_results_raise_typed_error(min_bars: object) -> None:
    registered = StudyRegistry().register(FakeStudy(min_bars_value=min_bars))

    with pytest.raises(StudyConfigError, match="positive integer"):
        registered.min_bars({})


@pytest.mark.parametrize("min_bars", (False, 1.0, "1"))
def test_additional_invalid_min_bars_results_raise_typed_error(min_bars: object) -> None:
    registered = StudyRegistry().register(FakeStudy(min_bars_value=min_bars))
    with pytest.raises(StudyConfigError, match="positive integer"):
        registered.min_bars({})


@pytest.mark.parametrize("value", (1, 2, 1000000))
def test_min_bars_accepts_builtin_positive_integers(value: int) -> None:
    registered = StudyRegistry().register(FakeStudy(min_bars_value=value))
    result = registered.min_bars({})
    assert type(result) is int
    assert result == value


def test_min_bars_rejects_int_subclasses_without_comparing_or_converting() -> None:
    class ZeroInt(int):
        def __lt__(self, other: object) -> bool:
            return False

    class ExplodingInt(int):
        def __lt__(self, other: object) -> bool:
            raise AssertionError("comparison called")

        def __ge__(self, other: object) -> bool:
            raise AssertionError("comparison called")

        def __int__(self) -> int:
            raise AssertionError("conversion called")

    for value in (ZeroInt(0), ExplodingInt(0), ExplodingInt(1)):
        registered = StudyRegistry().register(FakeStudy(min_bars_value=value))
        with pytest.raises(StudyConfigError, match="positive integer"):
            registered.min_bars({})


def test_min_bars_validates_every_result_from_the_study() -> None:
    study = FakeStudy(min_bars_value=2)
    registered = StudyRegistry().register(study)
    assert registered.min_bars({}) == 2
    study.min_bars_value = 0
    with pytest.raises(StudyConfigError, match="positive integer"):
        registered.min_bars({})
    study.min_bars_value = 1
    assert registered.min_bars({}) == 1


def test_study_output_preserves_none_and_uses_immutable_string_sequences() -> None:
    output = StudyOutput(
        status=StudyStatus.INSUFFICIENT_DATA,
        value=None,
        normalized_value=None,
        uncertainty=None,
        evidence=("not enough bars",),
        limitations=("descriptive only",),
    )

    assert output.value is None
    assert output.normalized_value is None
    assert output.uncertainty is None
    assert output.evidence == ("not enough bars",)
    assert output.limitations == ("descriptive only",)
    with pytest.raises(AttributeError):
        output.value = 0.0  # type: ignore[misc]


def test_study_output_fields_are_immutable_and_equality_and_repr_remain_useful() -> None:
    output = StudyOutput(
        StudyStatus.AVAILABLE,
        "state",
        1,
        2,
        Direction.BULLISH,
        3,
        ("evidence",),
        ("limitation",),
    )
    assert output == StudyOutput(
        status=StudyStatus.AVAILABLE,
        state="state",
        value=1.0,
        normalized_value=2.0,
        direction=Direction.BULLISH,
        uncertainty=3.0,
        evidence=("evidence",),
        limitations=("limitation",),
    )
    assert output != tuple(output)
    assert hash(output) == hash(
        StudyOutput(
            StudyStatus.AVAILABLE,
            "state",
            1.0,
            2.0,
            Direction.BULLISH,
            3.0,
            ("evidence",),
            ("limitation",),
        )
    )
    assert type(output.value) is float
    assert type(output.normalized_value) is float
    assert type(output.uncertainty) is float
    assert "StudyOutput(" in repr(output)
    assert "value=1.0" in repr(output)
    assert isinstance(output.evidence, tuple)
    assert isinstance(output.limitations, tuple)
    with pytest.raises(AttributeError):
        output.status = StudyStatus.UNAVAILABLE  # type: ignore[misc]
    with pytest.raises(AttributeError):
        output.state = "changed"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        output.value = 2.0  # type: ignore[misc]
    with pytest.raises(AttributeError):
        output.normalized_value = 3.0  # type: ignore[misc]
    with pytest.raises(AttributeError):
        output.direction = Direction.NEUTRAL  # type: ignore[misc]
    with pytest.raises(AttributeError):
        output.uncertainty = 4.0  # type: ignore[misc]
    with pytest.raises(AttributeError):
        output.evidence = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        output.limitations = ()  # type: ignore[misc]


def test_study_output_rejects_mutable_numeric_subclasses() -> None:
    class MutableFloat(float):
        pass

    mutable_number = MutableFloat(1.0)
    mutable_number.extra = []
    with pytest.raises(ValueError, match="finite number"):
        StudyOutput(status=StudyStatus.AVAILABLE, value=mutable_number)


@pytest.mark.parametrize("field", ("value", "normalized_value", "uncertainty"))
def test_study_output_rejects_spoofed_numbers_without_custom_methods(field: str) -> None:
    SpoofingMeta.equality_calls = 0
    for value in (SpoofedScalar(), SpoofedNumericScalar()):
        with pytest.raises(ValueError, match=field):
            StudyOutput(StudyStatus.AVAILABLE, **{field: value})
    assert SpoofingMeta.equality_calls == 0


def test_study_output_rejects_all_numeric_subclasses_and_overflow() -> None:
    class FractionSubclass(Fraction):
        def __float__(self) -> float:
            raise AssertionError("conversion called")

    for value in (HostileConfigInt(1), HostileConfigFloat(1), FractionSubclass(1, 2)):
        with pytest.raises(ValueError, match="finite number"):
            StudyOutput(StudyStatus.AVAILABLE, value=value)
    for value in (10**1000, Fraction(10**1000, 1)):
        with pytest.raises(ValueError, match="finite number") as exc:
            StudyOutput(StudyStatus.AVAILABLE, value=value)
        assert isinstance(exc.value.__cause__, OverflowError)


def test_study_output_rejects_subclass_shadowing_status() -> None:
    with pytest.raises(TypeError, match="subclass"):

        class ShadowedOutput(StudyOutput):
            status = StudyStatus.AVAILABLE  # type: ignore[assignment]


def test_study_output_has_no_writable_instance_storage() -> None:
    output = StudyOutput(StudyStatus.AVAILABLE)
    with pytest.raises(AttributeError):
        output.extra = "mutable"  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        output.__dict__  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        output[0] = output[0]  # type: ignore[index]


@pytest.mark.parametrize("field", ("state", "evidence", "limitations"))
@pytest.mark.parametrize("unhashable", (False, True))
def test_study_output_rejects_mutable_string_subclasses(field: str, unhashable: bool) -> None:
    class MutableString(str):
        hash_value = 1

        def __hash__(self) -> int:
            return self.hash_value

    class UnhashableString(str):
        __hash__ = None  # type: ignore[assignment]

    value = UnhashableString("text") if unhashable else MutableString("text")
    changes = {field: value if field == "state" else (value,)}
    with pytest.raises(ValueError, match=field):
        StudyOutput(StudyStatus.AVAILABLE, **changes)  # type: ignore[arg-type]


def test_study_output_snapshots_string_tuples_and_keeps_stable_hash() -> None:
    class MutableTuple(tuple):
        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(self.items)

    evidence = MutableTuple(("unused",))
    evidence.items = ["evidence"]
    limitations = MutableTuple(("unused",))
    limitations.items = ["limitation"]
    output = StudyOutput(
        StudyStatus.AVAILABLE, state="state", evidence=evidence, limitations=limitations
    )
    expected = StudyOutput(
        StudyStatus.AVAILABLE, state="state", evidence=("evidence",), limitations=("limitation",)
    )
    original_hash = hash(output)
    evidence.items.append("changed")
    limitations.items[0] = "changed"

    assert type(output.state) is str
    assert type(output.evidence) is tuple
    assert type(output.limitations) is tuple
    assert all(type(item) is str for item in output.evidence + output.limitations)
    assert output == expected
    assert hash(output) == original_hash == hash(expected)


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_study_output_copy_and_pickle_preserve_values(protocol: int) -> None:
    output = StudyOutput(
        StudyStatus.AVAILABLE,
        "state",
        1,
        None,
        Direction.BEARISH,
        0.5,
        ("evidence",),
        ("limitation",),
    )
    for restored in (
        copy.copy(output),
        copy.deepcopy(output),
        pickle.loads(pickle.dumps(output, protocol=protocol)),
    ):
        assert type(restored) is StudyOutput
        assert restored == output
        assert hash(restored) == hash(output)
        assert (
            restored.status,
            restored.state,
            restored.value,
            restored.normalized_value,
            restored.direction,
            restored.uncertainty,
            restored.evidence,
            restored.limitations,
        ) == (
            StudyStatus.AVAILABLE,
            "state",
            1.0,
            None,
            Direction.BEARISH,
            0.5,
            ("evidence",),
            ("limitation",),
        )


@pytest.mark.parametrize("direction", list(Direction))
def test_available_output_accepts_every_direction(direction: Direction) -> None:
    assert StudyOutput(StudyStatus.AVAILABLE, direction=direction).direction is direction


@pytest.mark.parametrize("status", (StudyStatus.INSUFFICIENT_DATA, StudyStatus.UNAVAILABLE))
@pytest.mark.parametrize("direction", (Direction.BULLISH, Direction.BEARISH, Direction.NEUTRAL))
def test_non_available_output_rejects_all_actionable_directions(
    status: StudyStatus, direction: Direction
) -> None:
    with pytest.raises(ValueError, match="NOT_APPLICABLE"):
        StudyOutput(status, direction=direction)


@pytest.mark.parametrize(
    ("status", "changes"),
    (
        (StudyStatus.INSUFFICIENT_DATA, {"direction": Direction.BULLISH}),
        (StudyStatus.INSUFFICIENT_DATA, {"value": 0.0}),
        (StudyStatus.INSUFFICIENT_DATA, {"normalized_value": 0.0}),
        (StudyStatus.INSUFFICIENT_DATA, {"uncertainty": 0.0}),
        (StudyStatus.INSUFFICIENT_DATA, {"state": "not-ready"}),
        (StudyStatus.UNAVAILABLE, {"direction": Direction.NEUTRAL}),
        (StudyStatus.UNAVAILABLE, {"value": 0.0}),
        (StudyStatus.UNAVAILABLE, {"normalized_value": 0.0}),
        (StudyStatus.UNAVAILABLE, {"uncertainty": 0.0}),
        (StudyStatus.UNAVAILABLE, {"state": "missing-feed"}),
    ),
)
def test_non_available_output_statuses_enforce_consistency(
    status: StudyStatus, changes: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        StudyOutput(status=status, **changes)  # type: ignore[arg-type]


def test_available_output_accepts_any_direction_and_normalizes_numbers_to_float() -> None:
    output = StudyOutput(
        status=StudyStatus.AVAILABLE,
        direction=Direction.BULLISH,
        value=2,
        normalized_value=Fraction(1, 3),
    )

    assert output.value == 2.0
    assert type(output.value) is float
    assert output.normalized_value == float(Fraction(1, 3))
    assert type(output.normalized_value) is float
    assert output.uncertainty is None


@pytest.mark.parametrize("status", (StudyStatus.ERROR, StudyStatus.STALE, "AVAILABLE"))
def test_study_output_rejects_status_outside_study_contract(status: object) -> None:
    with pytest.raises(ValueError, match="status"):
        StudyOutput(status=status)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    ("value", "normalized_value", "uncertainty"),
)
@pytest.mark.parametrize("number", (float("nan"), float("inf"), float("-inf"), True))
def test_study_output_rejects_non_finite_or_boolean_numbers(field: str, number: object) -> None:
    with pytest.raises(ValueError, match=field):
        StudyOutput(status=StudyStatus.AVAILABLE, **{field: number})


@pytest.mark.parametrize("number", ("1.0", 1 + 2j, object()))
def test_study_output_rejects_unsupported_numeric_types(number: object) -> None:
    with pytest.raises(ValueError, match="finite number"):
        StudyOutput(status=StudyStatus.AVAILABLE, value=number)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("evidence", ["mutable"]),
        ("evidence", ("valid", 1)),
        ("limitations", ["mutable"]),
        ("limitations", ("valid", None)),
    ),
)
def test_study_output_rejects_mutable_or_non_string_sequences(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        StudyOutput(status=StudyStatus.AVAILABLE, **{field: value})


def test_study_output_rejects_invalid_direction_and_state() -> None:
    with pytest.raises(ValueError, match="direction"):
        StudyOutput(status=StudyStatus.AVAILABLE, direction="NEUTRAL")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="state"):
        StudyOutput(status=StudyStatus.AVAILABLE, state=1)  # type: ignore[arg-type]
