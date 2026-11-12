"""Errors shared by historical-data validation components."""

from kankalar_gold_ai.core.contracts import DataQualityReport


class HistoricalDataQualityError(ValueError):
    """Raised when historical bars fail validation, preserving the quality report."""

    def __init__(self, report: DataQualityReport) -> None:
        self.report = report
        super().__init__(f"Bar quality check failed: {report.status} - {', '.join(report.reasons)}")
