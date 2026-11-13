"""Typed data and lifecycle contracts; implementations belong to later phases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
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
