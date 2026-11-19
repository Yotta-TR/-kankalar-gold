"""Confirmed strict-extreme swing events over validated completed bars."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime

from kankalar_gold_ai.core.contracts import (
    Direction,
    MarketBar,
    StudyEvent,
    StudyEventType,
    StudyStatus,
    study_event_id,
)
from kankalar_gold_ai.data.quality import bar_completion_utc
from kankalar_gold_ai.studies.base import ConfigValue, StudyComputationContext, StudyOutput
from kankalar_gold_ai.studies.errors import StudyConfigError


class ConfirmedSwingsStudy:
    study_id = "structure.swings"
    version = "0.1.0"
    required_timeframes = frozenset({"M1", "M5", "M15", "H1", "H4", "D1"})

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        if set(raw) != {"left_bars", "right_bars"}:
            raise StudyConfigError("configuration requires exactly left_bars and right_bars")
        for key in ("left_bars", "right_bars"):
            value = raw[key]
            if type(value) is not int or value <= 0:
                raise StudyConfigError(f"{key!r} must be a positive built-in integer")
        return {"left_bars": raw["left_bars"], "right_bars": raw["right_bars"]}

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        normalized = self.validate_config(config)
        left, right = normalized["left_bars"], normalized["right_bars"]
        assert type(left) is int and type(right) is int
        return left + right + 1

    def compute(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, ConfigValue],
    ) -> StudyOutput:
        raise RuntimeError("structure.swings requires StudyRunner computation context")

    def compute_with_context(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, ConfigValue],
        context: StudyComputationContext,
    ) -> StudyOutput:
        normalized = self.validate_config(config)
        left, right = normalized["left_bars"], normalized["right_bars"]
        assert type(left) is int and type(right) is int
        if context.timeframe not in self.required_timeframes or any(
            bar.timeframe != context.timeframe for bar in bars
        ):
            raise ValueError("bar timeframe does not match the computation context")

        for bar in bars:
            prices = (bar.open, bar.high, bar.low, bar.close)
            if (
                any(type(price) not in (int, float) or not math.isfinite(price) for price in prices)
                or bar.low <= 0
                or bar.low > min(bar.open, bar.close)
                or bar.high < max(bar.open, bar.close)
                or bar.low > bar.high
            ):
                raise ValueError("swing computation requires finite, valid OHLC bars")

        events: list[StudyEvent] = []
        for index in range(left, len(bars) - right):
            candidate = bars[index]
            left_window = bars[index - left : index]
            right_window = bars[index + 1 : index + right + 1]
            confirmation_bar = bars[index + right]
            known_at = bar_completion_utc(
                confirmation_bar.timestamp_utc,
                context.timeframe,
                timezone_name=context.calendar_timezone,
            )
            if known_at > context.as_of_utc:
                raise ValueError("swing confirmation is later than the analysis cutoff")

            if all(candidate.high > bar.high for bar in (*left_window, *right_window)):
                event_type = StudyEventType.SWING_HIGH
                events.append(
                    self._event(
                        candidate,
                        context,
                        event_type,
                        known_at,
                        candidate.high,
                        left,
                        right,
                    )
                )
            if all(candidate.low < bar.low for bar in (*left_window, *right_window)):
                event_type = StudyEventType.SWING_LOW
                events.append(
                    self._event(
                        candidate,
                        context,
                        event_type,
                        known_at,
                        candidate.low,
                        left,
                        right,
                    )
                )

        ordered_events = tuple(
            sorted(
                events,
                key=lambda event: (
                    event.known_at_utc,
                    event.pivot_time_utc,
                    event.event_type.value,
                    event.event_id,
                ),
            )
        )
        state = "SWINGS_CONFIRMED" if ordered_events else "NO_CONFIRMED_SWINGS"
        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state=state,
            direction=Direction.NOT_APPLICABLE,
            evidence=(
                f"left_bars={left}",
                f"right_bars={right}",
                f"event_count={len(ordered_events)}",
            ),
            events=ordered_events,
            limitations=(
                "Equal extremes are rejected; strict ties and flat plateaus produce no swing.",
                "Swings are descriptive events, not predictive signals or trading approval.",
            ),
        )

    def _event(
        self,
        pivot: MarketBar,
        context: StudyComputationContext,
        event_type: StudyEventType,
        known_at: datetime,
        price: float,
        left: int,
        right: int,
    ) -> StudyEvent:
        return StudyEvent(
            event_id=study_event_id(
                self.study_id,
                self.version,
                pivot.instrument,
                context.timeframe,
                event_type,
                pivot.timestamp_utc,
                left,
                right,
            ),
            study_id=self.study_id,
            version=self.version,
            instrument=pivot.instrument,
            timeframe=context.timeframe,
            event_type=event_type,
            pivot_time_utc=pivot.timestamp_utc,
            known_at_utc=known_at,
            price=price,
            left_bars=left,
            right_bars=right,
        )
