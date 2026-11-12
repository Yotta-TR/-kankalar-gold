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

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_utc", as_utc(self.timestamp_utc))
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("bar OHLC values are inconsistent")
        if self.low > self.high:
            raise ValueError("bar low must not exceed high")


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
