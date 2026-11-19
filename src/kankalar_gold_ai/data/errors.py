"""Errors shared by historical-data validation components."""

import re

from kankalar_gold_ai.core.contracts import DataQualityReport

_SOURCE_ID = re.compile(r"[a-z][a-z0-9_]{0,30}")
_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class EmptyHistoricalDataError(RuntimeError):
    """A provider completed a history request but returned no bars."""


class DataProviderError(RuntimeError):
    """A provider operation failed with its source and stable audit code."""

    def __init__(self, message: str, *, source_id: str, reason_code: str) -> None:
        if (
            type(source_id) is not str
            or _SOURCE_ID.fullmatch(source_id) is None
            or type(reason_code) is not str
            or _REASON_CODE.fullmatch(reason_code) is None
        ):
            raise ValueError("provider error source and reason code must be stable identifiers")
        self.source_id = source_id
        self.reason_code = reason_code
        super().__init__(message)


class HistoricalDataQualityError(ValueError):
    """Raised when historical bars fail validation, preserving the quality report."""

    def __init__(self, report: DataQualityReport) -> None:
        self.report = report
        super().__init__(f"Bar quality check failed: {report.status} - {', '.join(report.reasons)}")
