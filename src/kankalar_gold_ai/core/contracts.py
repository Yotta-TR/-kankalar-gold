"""Typed data and lifecycle contracts; implementations belong to later phases."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from fractions import Fraction
from typing import Protocol


def as_utc(value: datetime) -> datetime:
    """Convert an aware timestamp to UTC and reject ambiguous naive timestamps."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


class StudyStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    ERROR = "ERROR"


class Direction(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class StudyEventType(StrEnum):
    SWING_HIGH = "SWING_HIGH"
    SWING_LOW = "SWING_LOW"


class StructureBreakType(StrEnum):
    INITIAL_BREAK = "INITIAL_BREAK"
    BOS = "BOS"
    CHOCH = "CHOCH"


class StructureZoneType(StrEnum):
    SUPPORT = "SUPPORT"
    RESISTANCE = "RESISTANCE"


class StructureSweepSide(StrEnum):
    HIGH_SIDE = "HIGH_SIDE"
    LOW_SIDE = "LOW_SIDE"


class FailedBreakoutType(StrEnum):
    UPSIDE = "UPSIDE"
    DOWNSIDE = "DOWNSIDE"


class StudyEventConflictError(RuntimeError):
    """A stable study-event identity was presented with a different payload."""


_CALENDAR_CONTEXT_ID_PREFIX = "calendar-context-v1:sha256:"


def _is_calendar_context_id(value: object) -> bool:
    return (
        type(value) is str
        and value.startswith(_CALENDAR_CONTEXT_ID_PREFIX)
        and len(value) == len(_CALENDAR_CONTEXT_ID_PREFIX) + 64
        and all(character in "0123456789abcdef" for character in value[-64:])
    )


def study_event_id(
    study_id: str,
    version: str,
    instrument: str,
    timeframe: str,
    event_type: StudyEventType,
    pivot_time_utc: datetime,
    left_bars: int,
    right_bars: int,
    calendar_context_id: str | None = None,
) -> str:
    """Return a deterministic ID for an event, optionally scoped to its calendar context."""
    if calendar_context_id is not None and not _is_calendar_context_id(calendar_context_id):
        raise ValueError("calendar_context_id must be a valid calendar context identity or None")
    identity_fields = (
        study_id,
        version,
        instrument,
        timeframe,
        event_type.value,
        as_utc(pivot_time_utc).isoformat().replace("+00:00", "Z"),
        left_bars,
        right_bars,
    )
    identity = (
        identity_fields if calendar_context_id is None else (*identity_fields, calendar_context_id)
    )
    canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    event_id = f"study-event-v1:sha256:{digest}"
    return event_id if calendar_context_id is None else f"{event_id}:{calendar_context_id}"


class RiskOutcome(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class SignalState(StrEnum):
    CREATED = "CREATED"
    WATCHING = "WATCHING"
    TRIGGERED = "TRIGGERED"
    ACTIVE_SIMULATION = "ACTIVE_SIMULATION"
    TP1 = "TP1"
    TP2 = "TP2"
    STOP = "STOP"
    TIME_EXIT = "TIME_EXIT"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class MarketBar:
    instrument: str
    timeframe: str
    timestamp_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    source: str
    tick_volume: int | None = None
    real_volume: int | None = None
    spread_points: int | None = None
    retrieved_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_utc", as_utc(self.timestamp_utc))
        if self.retrieved_at_utc is not None:
            object.__setattr__(self, "retrieved_at_utc", as_utc(self.retrieved_at_utc))
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("bar OHLC values are inconsistent")
        if self.low > self.high:
            raise ValueError("bar low must not exceed high")


@dataclass(frozen=True, slots=True)
class StudyEvent:
    """Immutable, point-in-time event emitted by a versioned study."""

    event_id: str
    study_id: str
    version: str
    instrument: str
    timeframe: str
    event_type: StudyEventType
    pivot_time_utc: datetime
    known_at_utc: datetime
    price: float
    left_bars: int
    right_bars: int

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("study_id", self.study_id),
            ("version", self.version),
            ("instrument", self.instrument),
            ("timeframe", self.timeframe),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{name} must be a non-empty built-in string")
        if type(self.event_type) is not StudyEventType:
            raise ValueError("event_type must be a StudyEventType")
        pivot_time = as_utc(self.pivot_time_utc)
        known_at = as_utc(self.known_at_utc)
        if known_at <= pivot_time:
            raise ValueError("known_at_utc must be later than pivot_time_utc")
        if type(self.left_bars) is not int or self.left_bars <= 0:
            raise ValueError("left_bars must be a positive built-in integer")
        if type(self.right_bars) is not int or self.right_bars <= 0:
            raise ValueError("right_bars must be a positive built-in integer")
        if type(self.price) not in (int, float) or not math.isfinite(self.price) or self.price <= 0:
            raise ValueError("price must be a finite positive built-in number")
        object.__setattr__(self, "pivot_time_utc", pivot_time)
        object.__setattr__(self, "known_at_utc", known_at)
        object.__setattr__(self, "price", float(self.price))
        expected_id = study_event_id(
            self.study_id,
            self.version,
            self.instrument,
            self.timeframe,
            self.event_type,
            pivot_time,
            self.left_bars,
            self.right_bars,
        )
        context_marker = ":calendar-context-v1:sha256:"
        _, marker, context_digest = self.event_id.rpartition(context_marker)
        context_id = f"calendar-context-v1:sha256:{context_digest}"
        has_valid_context = bool(marker) and _is_calendar_context_id(context_id)
        if has_valid_context:
            has_valid_context = self.event_id == study_event_id(
                self.study_id,
                self.version,
                self.instrument,
                self.timeframe,
                self.event_type,
                pivot_time,
                self.left_bars,
                self.right_bars,
                calendar_context_id=context_id,
            )
        if self.event_id != expected_id and not has_valid_context:
            raise ValueError("event_id does not match the canonical event identity")


def structure_break_event_id(
    study_id: str,
    version: str,
    instrument: str,
    timeframe: str,
    reference_swing_id: str,
    break_bar_time_utc: datetime,
    calendar_context_id: str,
) -> str:
    """Return a stable identity scoped to the reference swing and calendar."""
    if not _is_calendar_context_id(calendar_context_id):
        raise ValueError("invalid calendar context identity")
    fields = (
        study_id,
        version,
        instrument,
        timeframe,
        reference_swing_id,
        as_utc(break_bar_time_utc).isoformat().replace("+00:00", "Z"),
        calendar_context_id,
    )
    canonical = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    return f"structure-break-v1:sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class StructureBreakEvent:
    """An immutable completed-close break of a previously confirmed swing."""

    event_id: str
    study_id: str
    version: str
    instrument: str
    timeframe: str
    reference_swing_id: str
    broken_level: float
    break_bar_time_utc: datetime
    known_at_utc: datetime
    direction: Direction
    classification: StructureBreakType
    calendar_context_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("study_id", self.study_id),
            ("version", self.version),
            ("instrument", self.instrument),
            ("timeframe", self.timeframe),
            ("reference_swing_id", self.reference_swing_id),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{name} must be a non-empty built-in string")
        if type(self.direction) is not Direction or self.direction not in (
            Direction.BULLISH,
            Direction.BEARISH,
        ):
            raise ValueError("direction must be BULLISH or BEARISH")
        if type(self.classification) is not StructureBreakType:
            raise ValueError("classification must be a StructureBreakType")
        if (
            type(self.broken_level) not in (int, float)
            or not math.isfinite(self.broken_level)
            or self.broken_level <= 0
        ):
            raise ValueError("broken_level must be a finite positive built-in number")
        bar_time = as_utc(self.break_bar_time_utc)
        known_at = as_utc(self.known_at_utc)
        if known_at <= bar_time:
            raise ValueError("known_at_utc must be later than break_bar_time_utc")
        object.__setattr__(self, "break_bar_time_utc", bar_time)
        object.__setattr__(self, "known_at_utc", known_at)
        object.__setattr__(self, "broken_level", float(self.broken_level))
        if self.event_id != structure_break_event_id(
            self.study_id,
            self.version,
            self.instrument,
            self.timeframe,
            self.reference_swing_id,
            bar_time,
            self.calendar_context_id,
        ):
            raise ValueError("event_id does not match the canonical structure-break identity")


def structure_zone_id(
    study_id: str,
    version: str,
    instrument: str,
    timeframe: str,
    zone_type: StructureZoneType,
    member_ids: tuple[str, ...],
    member_prices: tuple[float, ...],
    known_at_utc: datetime,
    lookback_bars: int,
    tolerance_bps: float,
    calendar_context_id: str,
) -> str:
    """Identify an immutable zone by its complete membership and calculation context."""
    fields = (
        study_id,
        version,
        instrument,
        timeframe,
        zone_type.value,
        tuple(zip(member_ids, member_prices)),
        as_utc(known_at_utc).isoformat().replace("+00:00", "Z"),
        lookback_bars,
        tolerance_bps,
        calendar_context_id,
    )
    canonical = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return f"structure-zone-v1:sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class StructureZone:
    """Immutable descriptive zone with its ordered confirmed-swing references."""

    event_id: str
    study_id: str
    version: str
    instrument: str
    timeframe: str
    zone_type: StructureZoneType
    member_ids: tuple[str, ...]
    member_prices: tuple[float, ...]
    lower_price: float
    upper_price: float
    known_at_utc: datetime
    lookback_bars: int
    tolerance_bps: float
    calendar_context_id: str

    def __post_init__(self) -> None:
        for value in (self.event_id, self.study_id, self.version, self.instrument, self.timeframe):
            if type(value) is not str or not value:
                raise ValueError("zone identity fields must be non-empty built-in strings")
        if type(self.zone_type) is not StructureZoneType:
            raise ValueError("zone_type must be a StructureZoneType")
        if (
            type(self.member_ids) is not tuple
            or len(self.member_ids) < 2
            or any(type(item) is not str or not item for item in self.member_ids)
            or len(set(self.member_ids)) != len(self.member_ids)
            or type(self.member_prices) is not tuple
            or len(self.member_prices) != len(self.member_ids)
        ):
            raise ValueError("zone requires at least two distinct swing references and prices")
        if any(
            type(price) not in (int, float) or not math.isfinite(price) or price <= 0
            for price in self.member_prices
        ):
            raise ValueError("zone member prices must be finite positive numbers")
        prices = tuple(float(price) for price in self.member_prices)
        if tuple(sorted(zip(prices, self.member_ids))) != tuple(zip(prices, self.member_ids)):
            raise ValueError("zone members must be ordered by price and ID")
        if self.lower_price != prices[0] or self.upper_price != prices[-1]:
            raise ValueError("zone bounds must match member prices")
        if type(self.lower_price) not in (int, float) or type(self.upper_price) not in (int, float):
            raise ValueError("zone bounds must be built-in numbers")
        if type(self.lookback_bars) is not int or self.lookback_bars <= 0:
            raise ValueError("lookback_bars must be a positive built-in integer")
        if (
            type(self.tolerance_bps) not in (int, float)
            or not math.isfinite(self.tolerance_bps)
            or self.tolerance_bps <= 0
        ):
            raise ValueError("tolerance_bps must be finite and positive")
        if not _is_calendar_context_id(self.calendar_context_id):
            raise ValueError("invalid calendar context identity")
        known_at = as_utc(self.known_at_utc)
        tolerance = float(self.tolerance_bps)
        if (Fraction(str(prices[-1])) - Fraction(str(prices[0]))) * 10000 > Fraction(
            str(prices[0])
        ) * Fraction(str(tolerance)):
            raise ValueError("zone members exceed the anchor tolerance")
        object.__setattr__(self, "member_prices", prices)
        object.__setattr__(self, "lower_price", prices[0])
        object.__setattr__(self, "upper_price", prices[-1])
        object.__setattr__(self, "known_at_utc", known_at)
        object.__setattr__(self, "tolerance_bps", tolerance)
        if self.event_id != structure_zone_id(
            self.study_id,
            self.version,
            self.instrument,
            self.timeframe,
            self.zone_type,
            self.member_ids,
            prices,
            known_at,
            self.lookback_bars,
            tolerance,
            self.calendar_context_id,
        ):
            raise ValueError("event_id does not match the canonical structure-zone identity")


def failed_breakout_event_id(
    study_id: str,
    version: str,
    instrument: str,
    timeframe: str,
    event_type: FailedBreakoutType,
    breakout_time_utc: datetime,
    reclaim_time_utc: datetime,
    lookback_bars: int,
    reclaim_window_bars: int,
    breakout_buffer_bps: float,
    calendar_context_id: str,
) -> str:
    """Identify a failed breakout independently of later snapshot extensions."""
    fields = (
        study_id,
        version,
        instrument,
        timeframe,
        event_type.value,
        as_utc(breakout_time_utc).isoformat(),
        as_utc(reclaim_time_utc).isoformat(),
        lookback_bars,
        reclaim_window_bars,
        float(breakout_buffer_bps),
        calendar_context_id,
    )
    canonical = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return f"failed-breakout-v1:sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class StructureFailedBreakoutEvent:
    """A completed close breakout followed by a close inside its frozen range."""

    event_id: str
    study_id: str
    version: str
    instrument: str
    timeframe: str
    event_type: FailedBreakoutType
    range_start_utc: datetime
    range_end_utc: datetime
    range_high: float
    range_low: float
    breakout_time_utc: datetime
    breakout_close: float
    reclaim_time_utc: datetime
    reclaim_close: float
    known_at_utc: datetime
    lookback_bars: int
    reclaim_window_bars: int
    breakout_buffer_bps: float
    calendar_context_id: str

    def __post_init__(self) -> None:
        for value in (self.event_id, self.study_id, self.version, self.instrument, self.timeframe):
            if type(value) is not str or not value:
                raise ValueError("failed-breakout identity fields must be non-empty strings")
        if type(self.event_type) is not FailedBreakoutType:
            raise ValueError("event_type must be a FailedBreakoutType")
        if not _is_calendar_context_id(self.calendar_context_id):
            raise ValueError("invalid calendar context identity")
        for name, timestamp in (
            ("range_start_utc", self.range_start_utc),
            ("range_end_utc", self.range_end_utc),
            ("breakout_time_utc", self.breakout_time_utc),
            ("reclaim_time_utc", self.reclaim_time_utc),
            ("known_at_utc", self.known_at_utc),
        ):
            if type(timestamp) is not datetime:
                raise ValueError(f"{name} must be a datetime")
            object.__setattr__(self, name, as_utc(timestamp))
        if not (
            self.range_start_utc
            <= self.range_end_utc
            < self.breakout_time_utc
            < self.reclaim_time_utc
            < self.known_at_utc
        ):
            raise ValueError("invalid failed-breakout chronology")
        for count in (self.lookback_bars, self.reclaim_window_bars):
            if type(count) is not int or count < 1:
                raise ValueError("window lengths must be positive built-in integers")
        for name, price in (
            ("range_high", self.range_high),
            ("range_low", self.range_low),
            ("breakout_close", self.breakout_close),
            ("reclaim_close", self.reclaim_close),
        ):
            if type(price) not in (int, float) or not math.isfinite(price) or price <= 0:
                raise ValueError(f"{name} must be a finite positive number")
            object.__setattr__(self, name, float(price))
        buffer = self.breakout_buffer_bps
        if type(buffer) not in (int, float) or not math.isfinite(buffer) or buffer < 0:
            raise ValueError("breakout_buffer_bps must be finite and non-negative")
        object.__setattr__(self, "breakout_buffer_bps", float(buffer))
        if self.range_low >= self.range_high or not (
            self.range_low <= self.reclaim_close <= self.range_high
        ):
            raise ValueError("invalid range or reclaim close")
        boundary = (
            self.range_high if self.event_type is FailedBreakoutType.UPSIDE else self.range_low
        )
        distance = (
            Fraction(str(self.breakout_close)) - Fraction(str(boundary))
            if self.event_type is FailedBreakoutType.UPSIDE
            else Fraction(str(boundary)) - Fraction(str(self.breakout_close))
        )
        if distance * 10000 <= Fraction(str(boundary)) * Fraction(str(buffer)):
            raise ValueError("breakout close must exceed its buffered boundary")
        if self.event_id != failed_breakout_event_id(
            self.study_id,
            self.version,
            self.instrument,
            self.timeframe,
            self.event_type,
            self.breakout_time_utc,
            self.reclaim_time_utc,
            self.lookback_bars,
            self.reclaim_window_bars,
            self.breakout_buffer_bps,
            self.calendar_context_id,
        ):
            raise ValueError("event_id does not match the failed-breakout identity")


def structure_sweep_event_id(
    study_id: str,
    version: str,
    instrument: str,
    timeframe: str,
    reference_zone_id: str,
    sweep_bar_time_utc: datetime,
    min_sweep_bps: float,
) -> str:
    """Identify a sweep by its zone, candle, study, market and threshold."""
    fields = (
        study_id,
        version,
        instrument,
        timeframe,
        reference_zone_id,
        as_utc(sweep_bar_time_utc).isoformat().replace("+00:00", "Z"),
        float(min_sweep_bps),
    )
    canonical = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return f"structure-sweep-v1:sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class StructureSweepEvent:
    """Immutable completed-candle wick reclaim of a previously known zone."""

    event_id: str
    study_id: str
    version: str
    instrument: str
    timeframe: str
    reference_zone: StructureZone
    side: StructureSweepSide
    sweep_bar_time_utc: datetime
    known_at_utc: datetime
    previous_close: float
    wick_price: float
    close: float
    min_sweep_bps: float

    def __post_init__(self) -> None:
        for value in (self.event_id, self.study_id, self.version, self.instrument, self.timeframe):
            if type(value) is not str or not value:
                raise ValueError("sweep identity fields must be non-empty built-in strings")
        if type(self.reference_zone) is not StructureZone:
            raise ValueError("reference_zone must be a StructureZone")
        zone = self.reference_zone
        if (
            zone.study_id != "structure.sr_zones"
            or zone.version != "0.1.0"
            or (zone.instrument, zone.timeframe) != (self.instrument, self.timeframe)
        ):
            raise ValueError("reference zone must match the sweep market and zone study")
        if type(self.side) is not StructureSweepSide:
            raise ValueError("side must be a StructureSweepSide")
        bar_time, known_at = as_utc(self.sweep_bar_time_utc), as_utc(self.known_at_utc)
        if known_at <= bar_time or zone.known_at_utc > bar_time:
            raise ValueError("sweep must follow zone confirmation and complete after its open")
        for number in (self.previous_close, self.wick_price, self.close, self.min_sweep_bps):
            if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
                raise ValueError("sweep prices and threshold must be finite positive numbers")
        previous, wick, close, threshold = map(
            float, (self.previous_close, self.wick_price, self.close, self.min_sweep_bps)
        )
        if self.side is StructureSweepSide.HIGH_SIDE:
            level = zone.upper_price
            valid = (
                zone.zone_type is StructureZoneType.RESISTANCE
                and previous <= level < wick
                and close <= level
            )
            overshoot = Fraction(str(wick)) - Fraction(str(level))
        else:
            level = zone.lower_price
            valid = (
                zone.zone_type is StructureZoneType.SUPPORT
                and previous >= level > wick
                and close >= level
            )
            overshoot = Fraction(str(level)) - Fraction(str(wick))
        if not valid or overshoot * 10000 < Fraction(str(level)) * Fraction(str(threshold)):
            raise ValueError("sweep must penetrate and reclaim the zone at the threshold")
        object.__setattr__(self, "sweep_bar_time_utc", bar_time)
        object.__setattr__(self, "known_at_utc", known_at)
        object.__setattr__(self, "previous_close", previous)
        object.__setattr__(self, "wick_price", wick)
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "min_sweep_bps", threshold)
        if self.event_id != structure_sweep_event_id(
            self.study_id,
            self.version,
            self.instrument,
            self.timeframe,
            zone.event_id,
            bar_time,
            threshold,
        ):
            raise ValueError("event_id does not match the canonical sweep identity")


class DataStatus(StrEnum):
    VALID = "VALID"
    STALE = "STALE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class MarketQuote:
    instrument: str
    timestamp_utc: datetime
    bid: float
    ask: float
    last: float | None
    timestamp_milliseconds: int | None
    source: str
    retrieved_at_utc: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_utc", as_utc(self.timestamp_utc))
        object.__setattr__(self, "retrieved_at_utc", as_utc(self.retrieved_at_utc))

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class MarketTick:
    instrument: str
    timestamp_utc: datetime
    timestamp_milliseconds: int | None
    bid: float | None
    ask: float | None
    last: float | None
    volume: float | None
    flags: int | None
    source: str
    retrieved_at_utc: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_utc", as_utc(self.timestamp_utc))
        object.__setattr__(self, "retrieved_at_utc", as_utc(self.retrieved_at_utc))


@dataclass(frozen=True, slots=True)
class InstrumentSpecification:
    symbol: str
    source: str
    retrieved_at_utc: datetime
    digits: int | None = None
    point: float | None = None
    contract_size: float | None = None
    tick_size: float | None = None
    tick_value: float | None = None
    volume_min: float | None = None
    volume_max: float | None = None
    volume_step: float | None = None
    trade_mode: int | None = None
    stops_level: int | None = None
    freeze_level: int | None = None
    currency_profit: str | None = None
    currency_margin: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "retrieved_at_utc", as_utc(self.retrieved_at_utc))


@dataclass(frozen=True, slots=True)
class MarketDataHealth:
    status: DataStatus
    source: str
    checked_at_utc: datetime
    detail: str
    terminal_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "checked_at_utc", as_utc(self.checked_at_utc))


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    status: DataStatus
    checked_at_utc: datetime
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "checked_at_utc", as_utc(self.checked_at_utc))


@dataclass(frozen=True, slots=True)
class StudyResult:
    study_id: str
    version: str
    instrument: str
    timeframe: str
    as_of_utc: datetime
    status: StudyStatus
    state: str | None
    value: float | None
    normalized_value: float | None
    direction: Direction
    uncertainty: float | None
    inputs: dict[str, str | int | float | bool | None]
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]
    data_snapshot_id: str | None
    provenance: tuple[tuple[str, str], ...] = ()
    events: tuple[
        StudyEvent
        | StructureBreakEvent
        | StructureZone
        | StructureSweepEvent
        | StructureFailedBreakoutEvent,
        ...,
    ] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of_utc", as_utc(self.as_of_utc))


@dataclass(frozen=True, slots=True)
class StrategyProposal:
    proposal_id: str
    strategy_id: str
    instrument: str
    direction: Direction
    created_at_utc: datetime
    entry_trigger: str
    invalidation_reason: str
    expires_at_utc: datetime
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at_utc", as_utc(self.created_at_utc))
        object.__setattr__(self, "expires_at_utc", as_utc(self.expires_at_utc))


@dataclass(frozen=True, slots=True)
class RiskDecision:
    proposal_id: str
    outcome: RiskOutcome
    reason_codes: tuple[str, ...]
    mode: str = "virtual"


@dataclass(frozen=True, slots=True)
class SignalLifecycleEvent:
    event_id: str
    signal_id: str
    state: SignalState
    occurred_at_utc: datetime
    reason_code: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at_utc", as_utc(self.occurred_at_utc))


class MarketDataProvider(Protocol):
    """Read-only source of timestamped market bars."""

    def get_bars(
        self,
        instrument: str,
        timeframe: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[MarketBar, ...]: ...


class Persistence(Protocol):
    """Append-oriented persistence boundary for auditable research records."""

    def save_market_bars(self, bars: tuple[MarketBar, ...]) -> None: ...

    def save_study_result(self, result: StudyResult) -> None: ...

    def save_signal_event(self, event: SignalLifecycleEvent) -> None: ...
