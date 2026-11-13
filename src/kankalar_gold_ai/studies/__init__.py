"""Typed interfaces and registration for independently tested research studies."""

from kankalar_gold_ai.studies.base import BarStudy, ConfigValue, StudyOutput
from kankalar_gold_ai.studies.errors import (
    DuplicateStudyError,
    InvalidStudyError,
    StudyConfigError,
    StudyError,
    StudyExecutionError,
    StudyPersistenceError,
    StudyRequestError,
    UnknownStudyError,
)
from kankalar_gold_ai.studies.registry import RegisteredStudy, StudyRegistry
from kankalar_gold_ai.studies.runner import (
    DataFailure,
    StudyResultStore,
    StudyRunner,
    StudyRunRequest,
)

__all__ = [
    "BarStudy",
    "ConfigValue",
    "DataFailure",
    "DuplicateStudyError",
    "InvalidStudyError",
    "RegisteredStudy",
    "StudyConfigError",
    "StudyError",
    "StudyExecutionError",
    "StudyPersistenceError",
    "StudyRequestError",
    "StudyResultStore",
    "StudyRunRequest",
    "StudyRunner",
    "StudyOutput",
    "StudyRegistry",
    "UnknownStudyError",
]
