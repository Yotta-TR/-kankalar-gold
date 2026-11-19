"""Fail-closed bridge from historical retrieval to a single study run."""

from kankalar_gold_ai.core.contracts import DataQualityReport, DataStatus, StudyResult
from kankalar_gold_ai.data.calendar import SessionCalendar
from kankalar_gold_ai.data.errors import (
    DataProviderError,
    EmptyHistoricalDataError,
    HistoricalDataQualityError,
)
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.studies.runner import DataFailure, StudyRunner, StudyRunRequest


def run_pipeline_study(
    runner: StudyRunner,
    pipeline: HistoricalDataPipeline,
    request: StudyRunRequest,
    *,
    history_days: int,
    calendar: SessionCalendar | None = None,
) -> StudyResult:
    """Fetch cutoff-bound bars, or persist a non-computing failure result."""
    try:
        bars = pipeline.retrieve_validated_bars(
            request.instrument,
            request.timeframe,
            request.as_of_utc,
            history_days,
            calendar=calendar,
        )
    except HistoricalDataQualityError as error:
        report = error.report
    except EmptyHistoricalDataError as error:
        reasons = ["EMPTY_HISTORY"]
        if isinstance(error, DataProviderError):
            reasons.append(f"DATA_SOURCE_{error.source_id.upper()}")
        report = DataQualityReport(
            DataStatus.INSUFFICIENT_DATA,
            pipeline.checked_at_utc(),
            tuple(sorted(reasons)),
        )
    except DataProviderError as error:
        report = DataQualityReport(
            DataStatus.ERROR,
            pipeline.checked_at_utc(),
            tuple(sorted((error.reason_code, f"DATA_SOURCE_{error.source_id.upper()}"))),
        )
    else:
        return runner.run(request, bars)

    return runner.run_data_failure(
        request,
        DataFailure(
            instrument=request.instrument,
            timeframe=request.timeframe,
            as_of_utc=request.as_of_utc,
            report=report,
        ),
    )
