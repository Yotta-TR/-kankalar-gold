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
from kankalar_gold_ai.studies.multitimeframe import (
    AnalysisCompleteness,
    ComponentResultEnvelope,
    FreshnessPolicy,
    MultiTimeframeAnalysisRequest,
    MultiTimeframeAnalysisResult,
    MultiTimeframePreflight,
    PreflightComponent,
    PreflightEligibility,
    StudyExecutionSpec,
    preflight_multi_timeframe_analysis,
)
from kankalar_gold_ai.studies.registry import RegisteredStudy, StudyRegistry
from kankalar_gold_ai.studies.runner import (
    DataFailure,
    PreparedStudyRun,
    StudyResultStore,
    StudyRunner,
    StudyRunRequest,
)

__all__ = [
    "BarStudy",
    "ConfigValue",
    "AnalysisCompleteness",
    "ComponentResultEnvelope",
    "DataFailure",
    "DuplicateStudyError",
    "FreshnessPolicy",
    "InvalidStudyError",
    "MultiTimeframeAnalysisRequest",
    "MultiTimeframeAnalysisResult",
    "MultiTimeframePreflight",
    "PreflightComponent",
    "PreflightEligibility",
    "PreparedStudyRun",
    "RegisteredStudy",
    "StudyConfigError",
    "StudyError",
    "StudyExecutionError",
    "StudyPersistenceError",
    "StudyRequestError",
    "StudyExecutionSpec",
    "StudyResultStore",
    "StudyRunner",
    "StudyRunRequest",
    "StudyOutput",
    "StudyRegistry",
    "UnknownStudyError",
    "preflight_multi_timeframe_analysis",
]
