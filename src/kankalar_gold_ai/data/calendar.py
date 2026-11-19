"""Explicit broker/instrument session calendars with UTC interval classification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kankalar_gold_ai.core.contracts import MarketBar, as_utc


class SessionState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class WeeklySession:
    """One local-time session anchored to a weekday, ending that day or the next."""

    weekday: int
    opens_at: time
    closes_at: time
    close_day_offset: int = 0
    open_fold: int | None = None
    close_fold: int | None = None

    def __post_init__(self) -> None:
        if type(self.weekday) is not int or not 0 <= self.weekday <= 6:
            raise ValueError("weekday must be an integer from 0 to 6")
        for name, value in (("opens_at", self.opens_at), ("closes_at", self.closes_at)):
            if type(value) is not time or value.tzinfo is not None:
                raise ValueError(f"{name} must be a timezone-naive local time")
        if type(self.close_day_offset) is not int or self.close_day_offset not in (0, 1):
            raise ValueError("close_day_offset must be 0 or 1")
        if self.close_day_offset == 0 and self.closes_at <= self.opens_at:
            raise ValueError("same-day session close must be later than its open")
        for name, fold in (("open_fold", self.open_fold), ("close_fold", self.close_fold)):
            if fold is not None and (type(fold) is not int or fold not in (0, 1)):
                raise ValueError(f"{name} must be 0, 1, or None")


@dataclass(frozen=True, slots=True)
class CalendarClosure:
    """An exceptional closure with absolute UTC boundaries and an audit reason."""

    start_utc: datetime
    end_utc: datetime
    reason: str

    def __post_init__(self) -> None:
        start = as_utc(self.start_utc)
        end = as_utc(self.end_utc)
        if start >= end:
            raise ValueError("closure start must precede closure end")
        if type(self.reason) is not str or not self.reason.strip():
            raise ValueError("closure reason must be non-empty")
        object.__setattr__(self, "start_utc", start)
        object.__setattr__(self, "end_utc", end)


@dataclass(frozen=True, slots=True)
class CalendarProvenance:
    """Source identity and a meaningful reference for the supplied schedule."""

    source: str
    reference: str

    def __post_init__(self) -> None:
        if type(self.source) is not str or not self.source.strip():
            raise ValueError("calendar provenance source must be non-empty")
        if type(self.reference) is not str or not self.reference.strip():
            raise ValueError("calendar provenance reference must be non-empty")


@dataclass(frozen=True, slots=True)
class CalendarSegment:
    start_utc: datetime
    end_utc: datetime
    state: SessionState

    @property
    def duration(self) -> timedelta:
        return self.end_utc - self.start_utc


@dataclass(frozen=True, slots=True)
class CalendarClassification:
    """Tri-state summary plus exact segments/durations for mixed or unknown intervals."""

    state: SessionState
    segments: tuple[CalendarSegment, ...]
    open_duration: timedelta
    closed_duration: timedelta
    unknown_duration: timedelta


def _local_to_utc(value: datetime, zone: ZoneInfo, fold: int | None) -> datetime:
    candidates: list[datetime] = []
    for candidate_fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=candidate_fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if round_trip == value:
            instant = candidate.astimezone(timezone.utc)
            if instant not in candidates:
                candidates.append(instant)
    if not candidates:
        raise ValueError(f"calendar rule falls in a nonexistent local time: {value}")
    if len(candidates) > 1:
        if fold is None:
            raise ValueError(f"ambiguous local calendar time requires an explicit fold: {value}")
        return value.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
    return candidates[0]


def local_day_completion_utc(timestamp_utc: datetime, timezone_name: str) -> datetime:
    """Return next local midnight for a D1 bar opened at local midnight."""
    timestamp = as_utc(timestamp_utc)
    zone = ZoneInfo(timezone_name)
    local = timestamp.astimezone(zone)
    if local.timetz().replace(tzinfo=None) != time.min:
        raise ValueError("D1 calendar bar must open at local midnight")
    next_day = local.date() + timedelta(days=1)
    return _local_to_utc(datetime.combine(next_day, time.min), zone, None)


@dataclass(frozen=True, slots=True)
class SessionCalendar:
    """Versioned, scoped calendar; effective dates and verified coverage are distinct."""

    calendar_id: str
    version: str
    provider_id: str
    instrument_id: str
    provenance: CalendarProvenance
    timezone_name: str
    effective_start_utc: datetime
    effective_end_utc: datetime
    coverage_start_utc: datetime
    coverage_end_utc: datetime
    weekly_sessions: tuple[WeeklySession, ...]
    holidays: frozenset[date] = frozenset()
    exceptional_closures: tuple[CalendarClosure, ...] = ()

    def __post_init__(self) -> None:
        if type(self.calendar_id) is not str or not self.calendar_id.strip():
            raise ValueError("calendar_id must be non-empty")
        if type(self.version) is not str or not self.version.strip():
            raise ValueError("calendar version must be non-empty")
        if type(self.provider_id) is not str or not self.provider_id.strip():
            raise ValueError("provider_id must be non-empty")
        if type(self.instrument_id) is not str or not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if type(self.provenance) is not CalendarProvenance:
            raise ValueError("provenance must be a CalendarProvenance")
        if type(self.timezone_name) is not str or not self.timezone_name:
            raise ValueError("timezone_name must be an explicit IANA timezone")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"unknown IANA timezone: {self.timezone_name}") from error
        effective_start = as_utc(self.effective_start_utc)
        effective_end = as_utc(self.effective_end_utc)
        coverage_start = as_utc(self.coverage_start_utc)
        coverage_end = as_utc(self.coverage_end_utc)
        if effective_start >= effective_end:
            raise ValueError("effective start must precede effective end")
        if coverage_start >= coverage_end:
            raise ValueError("calendar coverage start must precede its end")
        if coverage_start < effective_start or coverage_end > effective_end:
            raise ValueError("calendar coverage must be within its effective interval")
        if type(self.weekly_sessions) is not tuple or any(
            type(session) is not WeeklySession for session in self.weekly_sessions
        ):
            raise ValueError("weekly_sessions must be a tuple of WeeklySession values")
        if type(self.holidays) is not frozenset or any(
            type(holiday) is not date or type(holiday) is datetime for holiday in self.holidays
        ):
            raise ValueError("holidays must be a frozenset of local calendar dates")
        if type(self.exceptional_closures) is not tuple or any(
            type(closure) is not CalendarClosure for closure in self.exceptional_closures
        ):
            raise ValueError("exceptional_closures must be a tuple of CalendarClosure values")
        object.__setattr__(self, "effective_start_utc", effective_start)
        object.__setattr__(self, "effective_end_utc", effective_end)
        object.__setattr__(self, "coverage_start_utc", coverage_start)
        object.__setattr__(self, "coverage_end_utc", coverage_end)

    def _session_intervals(
        self, start: datetime, end: datetime
    ) -> tuple[tuple[datetime, datetime], ...]:
        zone = ZoneInfo(self.timezone_name)
        local_start = start.astimezone(zone).replace(tzinfo=None)
        local_end = end.astimezone(zone).replace(tzinfo=None)
        local_first = local_start.date() - timedelta(days=1)
        local_last = local_end.date() + timedelta(days=1)
        intervals: list[tuple[datetime, datetime]] = []
        day = local_first
        while day <= local_last:
            for rule in self.weekly_sessions:
                if day.weekday() != rule.weekday:
                    continue
                close_day = day + timedelta(days=rule.close_day_offset)
                local_open = datetime.combine(day, rule.opens_at)
                local_close = datetime.combine(close_day, rule.closes_at)
                session_open_utc = _local_to_utc(local_open, zone, rule.open_fold)
                session_close_utc = _local_to_utc(local_close, zone, rule.close_fold)
                if session_close_utc <= session_open_utc:
                    raise ValueError("calendar session is not positive after timezone conversion")
                if session_close_utc <= start or session_open_utc >= end:
                    continue
                intervals.append((max(start, session_open_utc), min(end, session_close_utc)))
            day += timedelta(days=1)
        return tuple(intervals)

    def classify_interval(self, start_utc: datetime, end_utc: datetime) -> CalendarClassification:
        """Classify a half-open UTC interval; uncovered or mixed intervals remain UNKNOWN."""
        start = as_utc(start_utc)
        end = as_utc(end_utc)
        if start >= end:
            raise ValueError("classification start must precede its end")
        covered_start = max(start, self.effective_start_utc, self.coverage_start_utc)
        covered_end = min(end, self.effective_end_utc, self.coverage_end_utc)
        boundaries = {start, end}
        sessions: tuple[tuple[datetime, datetime], ...] = ()
        if covered_start < covered_end:
            zone = ZoneInfo(self.timezone_name)
            sessions = self._session_intervals(covered_start, covered_end)
            boundaries.update((covered_start, covered_end))
            for session_start, session_end in sessions:
                if covered_start < session_start < covered_end:
                    boundaries.add(session_start)
                if covered_start < session_end < covered_end:
                    boundaries.add(session_end)
            for closure in self.exceptional_closures:
                if covered_start < closure.start_utc < covered_end:
                    boundaries.add(closure.start_utc)
                if covered_start < closure.end_utc < covered_end:
                    boundaries.add(closure.end_utc)

            local_first = covered_start.astimezone(zone).date() - timedelta(days=1)
            local_last = covered_end.astimezone(zone).date() + timedelta(days=1)
            day = local_first
            while day <= local_last:
                if day in self.holidays:
                    for boundary_day in (day, day + timedelta(days=1)):
                        local_boundary = datetime.combine(boundary_day, time.min)
                        covered_local_start = covered_start.astimezone(zone).replace(tzinfo=None)
                        covered_local_end = covered_end.astimezone(zone).replace(tzinfo=None)
                        if covered_local_start < local_boundary < covered_local_end:
                            boundaries.add(_local_to_utc(local_boundary, zone, None))
                day += timedelta(days=1)

        ordered = sorted(boundaries)
        segments: list[CalendarSegment] = []
        for left, right in zip(ordered, ordered[1:]):
            midpoint = left + (right - left) / 2
            if midpoint < covered_start or midpoint >= covered_end:
                state = SessionState.UNKNOWN
            else:
                local_day = midpoint.astimezone(zone).date()
                in_exception = any(
                    closure.start_utc <= midpoint < closure.end_utc
                    for closure in self.exceptional_closures
                )
                in_holiday = local_day in self.holidays
                in_session = any(
                    session_start <= midpoint < session_end
                    for session_start, session_end in sessions
                )
                state = (
                    SessionState.OPEN
                    if in_session and not in_exception and not in_holiday
                    else SessionState.CLOSED
                )
            if segments and segments[-1].state is state and segments[-1].end_utc == left:
                previous = segments[-1]
                segments[-1] = CalendarSegment(previous.start_utc, right, state)
            else:
                segments.append(CalendarSegment(left, right, state))

        open_duration = sum(
            (segment.duration for segment in segments if segment.state is SessionState.OPEN),
            timedelta(),
        )
        closed_duration = sum(
            (segment.duration for segment in segments if segment.state is SessionState.CLOSED),
            timedelta(),
        )
        unknown_duration = sum(
            (segment.duration for segment in segments if segment.state is SessionState.UNKNOWN),
            timedelta(),
        )
        nonzero_states = {segment.state for segment in segments if segment.duration > timedelta(0)}
        state = next(iter(nonzero_states)) if len(nonzero_states) == 1 else SessionState.UNKNOWN
        return CalendarClassification(
            state,
            tuple(segments),
            open_duration,
            closed_duration,
            unknown_duration,
        )

    def next_local_day_boundary(self, timestamp_utc: datetime) -> datetime:
        """Return the next local midnight for a D1 bar opened at local midnight."""
        return local_day_completion_utc(timestamp_utc, self.timezone_name)

    def local_day_start_utc(self, local_day: date) -> datetime:
        """Return the UTC instant for an explicit local calendar-day start."""
        if type(local_day) is not date or type(local_day) is datetime:
            raise TypeError("local_day must be a date")
        zone = ZoneInfo(self.timezone_name)
        return _local_to_utc(datetime.combine(local_day, time.min), zone, None)

    def canonical_identity(self) -> dict[str, str]:
        configuration = {
            "provider_id": self.provider_id,
            "instrument_id": self.instrument_id,
            "provenance_source": self.provenance.source,
            "provenance_reference": self.provenance.reference,
            "timezone_name": self.timezone_name,
            "effective_start_utc": self.effective_start_utc.isoformat(),
            "effective_end_utc": self.effective_end_utc.isoformat(),
            "coverage_start_utc": self.coverage_start_utc.isoformat(),
            "coverage_end_utc": self.coverage_end_utc.isoformat(),
            "weekly_sessions": [
                {
                    "weekday": session.weekday,
                    "opens_at": session.opens_at.isoformat(),
                    "closes_at": session.closes_at.isoformat(),
                    "close_day_offset": session.close_day_offset,
                    "open_fold": session.open_fold,
                    "close_fold": session.close_fold,
                }
                for session in self.weekly_sessions
            ],
            "holidays": sorted(holiday.isoformat() for holiday in self.holidays),
            "exceptional_closures": [
                {
                    "start_utc": closure.start_utc.isoformat(),
                    "end_utc": closure.end_utc.isoformat(),
                    "reason": closure.reason,
                }
                for closure in self.exceptional_closures
            ],
        }
        serialized = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return {
            "id": self.calendar_id,
            "version": self.version,
            "provider_id": self.provider_id,
            "instrument_id": self.instrument_id,
            "provenance_source": self.provenance.source,
            "provenance_reference": self.provenance.reference,
            "timezone": self.timezone_name,
            "effective_start_utc": self.effective_start_utc.isoformat(),
            "effective_end_utc": self.effective_end_utc.isoformat(),
            "coverage_start_utc": self.coverage_start_utc.isoformat(),
            "coverage_end_utc": self.coverage_end_utc.isoformat(),
            "configuration_sha256": fingerprint,
        }

    def canonical_identity_json(self) -> str:
        """Return the complete identity in deterministic JSON form."""
        return json.dumps(self.canonical_identity(), sort_keys=True, separators=(",", ":"))


def validate_calendar_scope(
    calendar: SessionCalendar,
    bars: tuple[MarketBar, ...],
    *,
    expected_instrument: str | None = None,
) -> None:
    """Reject calendar scope mismatches before the calendar can classify gaps.

    An empty history can be checked against an explicitly requested instrument,
    but it cannot establish a provider/source identity. No provider is inferred
    in that case.
    """
    if expected_instrument is not None and calendar.instrument_id != expected_instrument:
        raise ValueError("calendar scope mismatch: instrument identity does not match")
    for bar in bars:
        if bar.instrument != calendar.instrument_id:
            raise ValueError("calendar scope mismatch: instrument identity does not match")
        if bar.source != calendar.provider_id:
            raise ValueError("calendar scope mismatch: provider/source identity does not match")
