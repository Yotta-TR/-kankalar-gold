"""Completed-close market structure breaks of confirmed swings."""

from collections.abc import Mapping

from kankalar_gold_ai.core.contracts import (
    Direction,
    MarketBar,
    StructureBreakEvent,
    StructureBreakType,
    StudyEvent,
    StudyEventType,
    StudyStatus,
    structure_break_event_id,
)
from kankalar_gold_ai.data.quality import bar_completion_utc
from kankalar_gold_ai.studies.base import ConfigValue, StudyComputationContext, StudyOutput
from kankalar_gold_ai.studies.swings import ConfirmedSwingsStudy


class BosChochStudy:
    study_id = "structure.bos_choch"
    version = "0.1.0"
    required_timeframes = ConfirmedSwingsStudy.required_timeframes

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        return ConfirmedSwingsStudy().validate_config(raw)

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        return ConfirmedSwingsStudy().min_bars(config) + 1

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        raise RuntimeError("structure.bos_choch requires StudyRunner computation context")

    def compute_with_context(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, ConfigValue],
        context: StudyComputationContext,
    ) -> StudyOutput:
        swing_output = ConfirmedSwingsStudy().compute_with_context(bars, config, context)
        if any(type(event) is not StudyEvent for event in swing_output.events):
            raise ValueError("confirmed swing study returned a non-swing event")
        swings = tuple(event for event in swing_output.events if isinstance(event, StudyEvent))
        consumed: set[str] = set()
        events: list[StructureBreakEvent] = []
        bias = Direction.NOT_APPLICABLE
        for previous, bar in zip(bars, bars[1:]):
            known_at = bar_completion_utc(
                bar.timestamp_utc, context.timeframe, timezone_name=context.calendar_timezone
            )
            if known_at > context.as_of_utc:
                raise ValueError("break candle completion is later than the analysis cutoff")
            rising = bar.close > previous.close
            falling = bar.close < previous.close
            candidates = [
                swing
                for swing in swings
                if swing.event_id not in consumed
                and swing.known_at_utc <= bar.timestamp_utc
                and (
                    (
                        rising
                        and swing.event_type is StudyEventType.SWING_HIGH
                        and previous.close <= swing.price < bar.close
                    )
                    or (
                        falling
                        and swing.event_type is StudyEventType.SWING_LOW
                        and bar.close < swing.price <= previous.close
                    )
                )
            ]
            if not candidates:
                continue
            nearest_level = (
                min(swing.price for swing in candidates)
                if rising
                else max(swing.price for swing in candidates)
            )
            nearest = [swing for swing in candidates if swing.price == nearest_level]
            swing = max(
                nearest,
                key=lambda item: (item.known_at_utc, item.pivot_time_utc, item.event_id),
            )
            consumed.update(swing.event_id for swing in candidates)
            direction = Direction.BULLISH if rising else Direction.BEARISH
            classification = (
                StructureBreakType.INITIAL_BREAK
                if bias is Direction.NOT_APPLICABLE
                else StructureBreakType.BOS
                if bias is direction
                else StructureBreakType.CHOCH
            )
            bias = direction
            events.append(
                StructureBreakEvent(
                    event_id=structure_break_event_id(
                        self.study_id,
                        self.version,
                        bar.instrument,
                        context.timeframe,
                        swing.event_id,
                        bar.timestamp_utc,
                        context.calendar_context_id,
                    ),
                    study_id=self.study_id,
                    version=self.version,
                    instrument=bar.instrument,
                    timeframe=context.timeframe,
                    reference_swing_id=swing.event_id,
                    broken_level=swing.price,
                    break_bar_time_utc=bar.timestamp_utc,
                    known_at_utc=known_at,
                    direction=direction,
                    classification=classification,
                    calendar_context_id=context.calendar_context_id,
                )
            )
        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state="STRUCTURE_BREAKS" if events else "NO_STRUCTURE_BREAKS",
            direction=bias,
            evidence=(f"event_count={len(events)}",),
            events=tuple(events),
            limitations=("Structure breaks describe historical closes, not trading approval.",),
        )
