"""Close-confirmed failures of breakouts from a frozen preceding range."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction

from kankalar_gold_ai.core.contracts import (
    Direction,
    FailedBreakoutType,
    MarketBar,
    StructureFailedBreakoutEvent,
    StudyStatus,
    failed_breakout_event_id,
)
from kankalar_gold_ai.data.quality import bar_completion_utc
from kankalar_gold_ai.studies.base import ConfigValue, StudyComputationContext, StudyOutput
from kankalar_gold_ai.studies.errors import StudyConfigError
from kankalar_gold_ai.studies.swings import ConfirmedSwingsStudy


@dataclass(frozen=True, slots=True)
class _Candidate:
    event_type: FailedBreakoutType
    breakout: MarketBar
    range_start: MarketBar
    range_end: MarketBar
    high: float
    low: float
    index: int


class RangeFailedBreakoutStudy:
    study_id = "structure.range_failed_breakout"
    version = "0.1.0"
    required_timeframes = ConfirmedSwingsStudy.required_timeframes

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        if set(raw) != {"lookback_bars", "reclaim_window_bars", "breakout_buffer_bps"}:
            raise StudyConfigError("configuration requires lookback, reclaim window and buffer")
        for key in ("lookback_bars", "reclaim_window_bars"):
            count = raw[key]
            if type(count) is not int or count < 1:
                raise StudyConfigError(f"{key} must be a positive built-in integer")
        buffer = raw["breakout_buffer_bps"]
        if type(buffer) not in (int, float):
            raise StudyConfigError("breakout_buffer_bps must be a finite non-negative number")
        assert isinstance(buffer, (int, float))
        try:
            normalized = float(buffer)
        except OverflowError as error:
            raise StudyConfigError("breakout_buffer_bps must be finite") from error
        if not math.isfinite(normalized) or normalized < 0:
            raise StudyConfigError("breakout_buffer_bps must be a finite non-negative number")
        return {**raw, "breakout_buffer_bps": normalized}

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        normalized = self.validate_config(config)
        lookback = normalized["lookback_bars"]
        assert type(lookback) is int
        return lookback + 1

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        raise RuntimeError(
            "structure.range_failed_breakout requires StudyRunner computation context"
        )

    def compute_with_context(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, ConfigValue],
        context: StudyComputationContext,
    ) -> StudyOutput:
        normalized = self.validate_config(config)
        lookback = normalized["lookback_bars"]
        window = normalized["reclaim_window_bars"]
        buffer = normalized["breakout_buffer_bps"]
        assert type(lookback) is int and type(window) is int and type(buffer) is float
        candidate: _Candidate | None = None
        events: list[StructureFailedBreakoutEvent] = []
        for index in range(lookback, len(bars)):
            bar = bars[index]
            completion = bar_completion_utc(
                bar.timestamp_utc, context.timeframe, timezone_name=context.calendar_timezone
            )
            if completion > context.as_of_utc:
                raise ValueError("candle completion is later than the analysis cutoff")
            if candidate is not None:
                if candidate.low <= bar.close <= candidate.high:
                    event_id = failed_breakout_event_id(
                        self.study_id,
                        self.version,
                        bar.instrument,
                        bar.timeframe,
                        candidate.event_type,
                        candidate.breakout.timestamp_utc,
                        bar.timestamp_utc,
                        lookback,
                        window,
                        buffer,
                        context.calendar_context_id,
                    )
                    events.append(
                        StructureFailedBreakoutEvent(
                            event_id,
                            self.study_id,
                            self.version,
                            bar.instrument,
                            bar.timeframe,
                            candidate.event_type,
                            candidate.range_start.timestamp_utc,
                            candidate.range_end.timestamp_utc,
                            candidate.high,
                            candidate.low,
                            candidate.breakout.timestamp_utc,
                            candidate.breakout.close,
                            bar.timestamp_utc,
                            bar.close,
                            completion,
                            lookback,
                            window,
                            buffer,
                            context.calendar_context_id,
                        )
                    )
                    candidate = None
                elif (
                    candidate.event_type is FailedBreakoutType.UPSIDE and bar.close < candidate.low
                ) or (
                    candidate.event_type is FailedBreakoutType.DOWNSIDE
                    and bar.close > candidate.high
                ):
                    candidate = None
                elif index - candidate.index >= window:
                    candidate = None
                continue
            history = bars[index - lookback : index]
            high = max(item.high for item in history)
            low = min(item.low for item in history)
            if high <= low or not low <= bars[index - 1].close <= high:
                continue
            high_distance = Fraction(str(bar.close)) - Fraction(str(high))
            low_distance = Fraction(str(low)) - Fraction(str(bar.close))
            threshold = Fraction(str(buffer))
            if high_distance * 10000 > Fraction(str(high)) * threshold:
                side = FailedBreakoutType.UPSIDE
            elif low_distance * 10000 > Fraction(str(low)) * threshold:
                side = FailedBreakoutType.DOWNSIDE
            else:
                continue
            candidate = _Candidate(side, bar, history[0], history[-1], high, low, index)
        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state="FAILED_BREAKOUTS_IDENTIFIED" if events else "NO_FAILED_BREAKOUTS",
            direction=Direction.NOT_APPLICABLE,
            evidence=(f"event_count={len(events)}",),
            limitations=(
                "Close-confirmed failed breakouts are descriptive, not trading recommendations.",
                "Volume-based strength is unavailable and unimplemented.",
            ),
            events=tuple(events),
        )
