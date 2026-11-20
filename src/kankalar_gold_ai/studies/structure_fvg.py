"""Three-candle descriptive price-imbalance events."""

from collections.abc import Mapping
from datetime import timedelta

from kankalar_gold_ai.core.contracts import (
    Direction,
    MarketBar,
    StructureFvgEvent,
    StudyStatus,
    structure_fvg_event_id,
)
from kankalar_gold_ai.data.calendar import SessionCalendar
from kankalar_gold_ai.data.quality import TIMEFRAME_DURATIONS, bar_completion_utc
from kankalar_gold_ai.studies.base import ConfigValue, StudyComputationContext, StudyOutput
from kankalar_gold_ai.studies.errors import StudyConfigError


class StructureFvgStudy:
    study_id = "structure.gaps_fvg"
    version = "0.1.0"
    required_timeframes = frozenset(TIMEFRAME_DURATIONS)

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        if raw:
            raise StudyConfigError("structure.gaps_fvg does not accept configuration")
        return {}

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        self.validate_config(config)
        return 3

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        raise RuntimeError("structure.gaps_fvg requires StudyRunner computation context")

    def compute_with_context(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, ConfigValue],
        context: StudyComputationContext,
    ) -> StudyOutput:
        self.validate_config(config)
        events: list[StructureFvgEvent] = []
        completions = tuple(
            bar_completion_utc(bar.timestamp_utc, context.timeframe, context.calendar)
            for bar in bars
        )
        if any(completion > context.as_of_utc for completion in completions):
            raise ValueError("candle completion is later than the analysis cutoff")

        for index in range(len(bars) - 2):
            first, middle, third = bars[index : index + 3]
            if context.calendar is not None and any(
                not _is_calendar_tradable_bar(item, context.timeframe, context.calendar)
                for item in (first, middle, third)
            ):
                continue
            if not _is_expected_successor(
                first, middle, context.timeframe, context.calendar
            ) or not _is_expected_successor(middle, third, context.timeframe, context.calendar):
                continue

            if third.low > first.high:
                direction = Direction.BULLISH
                lower, upper = first.high, third.low
            elif third.high < first.low:
                direction = Direction.BEARISH
                lower, upper = third.high, first.low
            else:
                continue

            known_at = completions[index + 2]
            event_id = structure_fvg_event_id(
                self.study_id,
                self.version,
                third.instrument,
                context.timeframe,
                direction,
                lower,
                upper,
                first.timestamp_utc,
                middle.timestamp_utc,
                third.timestamp_utc,
                known_at,
                third.source,
                context.calendar_context_id,
            )
            events.append(
                StructureFvgEvent(
                    event_id,
                    self.study_id,
                    self.version,
                    third.instrument,
                    context.timeframe,
                    direction,
                    lower,
                    upper,
                    first.timestamp_utc,
                    middle.timestamp_utc,
                    third.timestamp_utc,
                    known_at,
                    third.source,
                    context.calendar_context_id,
                )
            )

        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state="PRICE_IMBALANCES_IDENTIFIED" if events else "NO_PRICE_IMBALANCES",
            direction=Direction.NOT_APPLICABLE,
            evidence=(f"event_count={len(events)}",),
            limitations=(
                "FVG events describe price imbalances; they do not establish an order-book void "
                "or institutional activity.",
                "Mitigation/fill, merging, ranking, and out-of-sample predictive validation "
                "are not implemented.",
            ),
            events=tuple(events),
        )


def _is_expected_successor(
    previous: MarketBar,
    following: MarketBar,
    timeframe: str,
    calendar: SessionCalendar | None,
) -> bool:
    completion = bar_completion_utc(previous.timestamp_utc, timeframe, calendar)
    if following.timestamp_utc < completion:
        return False
    if following.timestamp_utc == completion:
        return True
    if calendar is None:
        return False
    classification = calendar.classify_interval(completion, following.timestamp_utc)
    return (
        classification.open_duration == timedelta()
        and classification.unknown_duration == timedelta()
        and classification.closed_duration == following.timestamp_utc - completion
    )


def _is_calendar_tradable_bar(bar: MarketBar, timeframe: str, calendar: SessionCalendar) -> bool:
    completion = bar_completion_utc(bar.timestamp_utc, timeframe, calendar)
    classification = calendar.classify_interval(bar.timestamp_utc, completion)
    return (
        classification.open_duration == completion - bar.timestamp_utc
        and classification.closed_duration == timedelta()
        and classification.unknown_duration == timedelta()
    )
