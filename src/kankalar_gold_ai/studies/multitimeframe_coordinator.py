"""Coordinate independent single-timeframe study runs after complete preflight."""

from kankalar_gold_ai.core.contracts import DataQualityReport, DataStatus
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.studies.multitimeframe import (
    ComponentResultEnvelope,
    MultiTimeframeAnalysisRequest,
    MultiTimeframeAnalysisResult,
    PreflightEligibility,
    preflight_multi_timeframe_analysis,
)
from kankalar_gold_ai.studies.runner import DataFailure, StudyRunner


class MultiTimeframeAnalysisCoordinator:
    """Execute individual runs in preflight order; persistence is not atomic."""

    def __init__(self, runner: StudyRunner) -> None:
        self._runner = runner

    def run(self, request: MultiTimeframeAnalysisRequest) -> MultiTimeframeAnalysisResult:
        """Preflight all inputs, then execute and persist each independent component.

        A later infrastructure or persistence failure propagates; earlier component
        results may already have been persisted.
        """
        preflight = preflight_multi_timeframe_analysis(request, self._runner)
        envelopes: list[ComponentResultEnvelope] = []
        for component in preflight.components:
            input_data = preflight.request.inputs_by_timeframe[component.timeframe]
            if isinstance(input_data, DataFailure):
                result = self._runner.run_data_failure(component.prepared.request, input_data)
            elif component.eligibility is PreflightEligibility.STALE:
                failure = DataFailure(
                    instrument=preflight.request.instrument,
                    timeframe=component.timeframe,
                    as_of_utc=preflight.request.as_of_utc,
                    report=DataQualityReport(
                        DataStatus.STALE,
                        preflight.request.as_of_utc,
                        ("FRESHNESS_MAX_AGE_EXCEEDED",),
                    ),
                )
                result = self._runner.run_data_failure(component.prepared.request, failure)
            else:
                assert isinstance(input_data, ValidatedBarSet)
                result = self._runner.run(component.prepared.request, input_data)
            envelopes.append(ComponentResultEnvelope(component, result))
        return MultiTimeframeAnalysisResult(preflight, tuple(envelopes))
