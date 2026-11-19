"""Validated historical OHLC bars with their quality and session-policy provenance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from kankalar_gold_ai.core.contracts import (
    DataQualityReport,
    DataStatus,
    MarketBar,
    as_utc,
)
from kankalar_gold_ai.data.bar_validation import bar_integrity_reasons
from kankalar_gold_ai.data.calendar import SessionCalendar
from kankalar_gold_ai.data.errors import HistoricalDataQualityError
from kankalar_gold_ai.data.quality import bar_completion_utc, validate_bars

ExpectedOpen = Callable[[datetime], bool]


def _utc_session_policy(policy: ExpectedOpen | None) -> ExpectedOpen | None:
    if policy is None:
        return None

    def checked(timestamp: datetime) -> bool:
        result = policy(as_utc(timestamp))
        if not isinstance(result, bool):
            raise TypeError("expected_open must return bool")
        return result

    return checked


@dataclass(frozen=True, slots=True, init=False)
class ValidatedBarSet:
    """Immutable, non-empty bars accepted by ``validate_bars`` and intrinsic checks.

    If bars span a gap, ``expected_open`` must be a deterministic, pure session
    policy over UTC timestamps. No production session calendar is provided here.
    Direct construction asserts the cutoff; it does not prove provider provenance.
    """

    instrument: str
    timeframe: str
    source: str
    bars: tuple[MarketBar, ...]
    report: DataQualityReport
    validated_at_utc: datetime
    cutoff_utc: datetime
    expected_open: ExpectedOpen | None
    calendar_id: str | None
    calendar_version: str | None
    calendar_timezone: str | None
    calendar_configuration_sha256: str | None
    calendar_identity_json: str | None

    def __init__(
        self,
        bars: tuple[MarketBar, ...],
        timeframe: str,
        validated_at_utc: datetime,
        *,
        cutoff_utc: datetime,
        expected_open: ExpectedOpen | None = None,
        expected_instrument: str | None = None,
        calendar: SessionCalendar | None = None,
    ) -> None:
        if not isinstance(bars, tuple):
            raise TypeError("bars must be an ordered tuple")
        now = as_utc(validated_at_utc)
        cutoff = as_utc(cutoff_utc)
        if cutoff > now:
            raise ValueError("cutoff_utc must not be later than validated_at_utc")
        session_policy = _utc_session_policy(expected_open)
        if any(not isinstance(bar, MarketBar) for bar in bars):
            raise TypeError("bars must contain only MarketBar instances")

        report = validate_bars(
            bars,
            timeframe,
            now,
            expected_open=session_policy,
            calendar=calendar,
            expected_instrument=expected_instrument,
        )
        integrity_reasons = bar_integrity_reasons(bars, timeframe, expected_instrument)
        if integrity_reasons:
            report = DataQualityReport(
                DataStatus.ERROR,
                now,
                tuple(sorted(set(report.reasons) | set(integrity_reasons))),
            )
        if report.status is not DataStatus.VALID:
            raise HistoricalDataQualityError(report)

        if any(bar_completion_utc(bar.timestamp_utc, timeframe, calendar) > cutoff for bar in bars):
            raise ValueError("input bars complete after cutoff_utc")

        first = bars[0]
        object.__setattr__(self, "instrument", first.instrument)
        object.__setattr__(self, "timeframe", timeframe)
        object.__setattr__(self, "source", first.source)
        object.__setattr__(self, "bars", bars)
        object.__setattr__(self, "report", report)
        object.__setattr__(self, "validated_at_utc", now)
        object.__setattr__(self, "cutoff_utc", cutoff)
        object.__setattr__(self, "expected_open", session_policy)
        object.__setattr__(self, "calendar_id", None if calendar is None else calendar.calendar_id)
        object.__setattr__(self, "calendar_version", None if calendar is None else calendar.version)
        identity = None if calendar is None else calendar.canonical_identity()
        object.__setattr__(
            self, "calendar_timezone", None if calendar is None else calendar.timezone_name
        )
        object.__setattr__(
            self,
            "calendar_configuration_sha256",
            None if identity is None else identity["configuration_sha256"],
        )
        object.__setattr__(
            self,
            "calendar_identity_json",
            None if calendar is None else calendar.canonical_identity_json(),
        )

    @classmethod
    def from_bars(
        cls,
        bars: tuple[MarketBar, ...],
        timeframe: str,
        validated_at_utc: datetime,
        *,
        cutoff_utc: datetime,
        expected_open: ExpectedOpen | None = None,
        expected_instrument: str | None = None,
        calendar: SessionCalendar | None = None,
    ) -> ValidatedBarSet:
        """Validate bars and raise with the full report unless quality is VALID."""
        return cls(
            bars,
            timeframe,
            validated_at_utc,
            cutoff_utc=cutoff_utc,
            expected_open=expected_open,
            expected_instrument=expected_instrument,
            calendar=calendar,
        )
