"""Typed interfaces and registration for independently tested research studies."""

from kankalar_gold_ai.studies.base import (
    BarStudy,
    ConfigValue,
    StudyComputationContext,
    StudyOutput,
)
from kankalar_gold_ai.studies.bos_choch import BosChochStudy
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
from kankalar_gold_ai.studies.liquidity_sweep import LiquiditySweepStudy
from kankalar_gold_ai.studies.multitimeframe import (
    AnalysisCompleteness,
    CalendarAwareFreshnessPolicy,
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
from kankalar_gold_ai.studies.multitimeframe_coordinator import MultiTimeframeAnalysisCoordinator
from kankalar_gold_ai.studies.pipeline_execution import run_pipeline_study
from kankalar_gold_ai.studies.range_failed_breakout import RangeFailedBreakoutStudy
from kankalar_gold_ai.studies.registry import RegisteredStudy, StudyRegistry
from kankalar_gold_ai.studies.runner import (
    DataFailure,
    PreparedStudyRun,
    StudyResultStore,
    StudyRunner,
    StudyRunRequest,
)
from kankalar_gold_ai.studies.sr_zones import SupportResistanceZonesStudy
from kankalar_gold_ai.studies.structure_fvg import StructureFvgStudy
from kankalar_gold_ai.studies.swings import ConfirmedSwingsStudy

__all__ = [
    "BarStudy",
    "BosChochStudy",
    "ConfigValue",
    "AnalysisCompleteness",
    "CalendarAwareFreshnessPolicy",
    "ComponentResultEnvelope",
    "ConfirmedSwingsStudy",
    "DataFailure",
    "DuplicateStudyError",
    "FreshnessPolicy",
    "InvalidStudyError",
    "LiquiditySweepStudy",
    "MultiTimeframeAnalysisCoordinator",
    "MultiTimeframeAnalysisRequest",
    "MultiTimeframeAnalysisResult",
    "MultiTimeframePreflight",
    "PreflightComponent",
    "PreflightEligibility",
    "PreparedStudyRun",
    "RangeFailedBreakoutStudy",
    "RegisteredStudy",
    "StudyConfigError",
    "StudyComputationContext",
    "StudyError",
    "StudyExecutionError",
    "StudyPersistenceError",
    "StudyRequestError",
    "StudyExecutionSpec",
    "StudyResultStore",
    "StudyRunner",
    "StudyRunRequest",
    "StructureFvgStudy",
    "SupportResistanceZonesStudy",
    "run_pipeline_study",
    "StudyOutput",
    "StudyRegistry",
    "UnknownStudyError",
    "preflight_multi_timeframe_analysis",
]
