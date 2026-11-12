"""Typed interfaces and registration for independently tested research studies."""

from kankalar_gold_ai.studies.base import BarStudy, ConfigValue, StudyOutput
from kankalar_gold_ai.studies.errors import (
    DuplicateStudyError,
    InvalidStudyError,
    StudyConfigError,
    StudyError,
    UnknownStudyError,
)
from kankalar_gold_ai.studies.registry import RegisteredStudy, StudyRegistry

__all__ = [
    "BarStudy",
    "ConfigValue",
    "DuplicateStudyError",
    "InvalidStudyError",
    "RegisteredStudy",
    "StudyConfigError",
    "StudyError",
    "StudyOutput",
    "StudyRegistry",
    "UnknownStudyError",
]
