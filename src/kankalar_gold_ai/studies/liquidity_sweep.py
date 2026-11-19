"""Completed-candle wick reclaims of point-in-time support/resistance zones."""

import math
from collections.abc import Mapping
from datetime import datetime
from fractions import Fraction

from kankalar_gold_ai.core.contracts import (
    Direction,
    MarketBar,
    StructureSweepEvent,
    StructureSweepSide,
    StructureZone,
    StructureZoneType,
    StudyEvent,
    StudyStatus,
    structure_sweep_event_id,
)
from kankalar_gold_ai.data.quality import bar_completion_utc
from kankalar_gold_ai.studies.base import ConfigValue, StudyComputationContext, StudyOutput
from kankalar_gold_ai.studies.errors import StudyConfigError
from kankalar_gold_ai.studies.sr_zones import SupportResistanceZonesStudy
from kankalar_gold_ai.studies.swings import ConfirmedSwingsStudy


class LiquiditySweepStudy:
    study_id = "structure.liquidity_sweep"
    version = "0.1.0"
    required_timeframes = SupportResistanceZonesStudy.required_timeframes

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        if set(raw) != {
            "left_bars",
            "right_bars",
            "lookback_bars",
            "tolerance_bps",
            "min_sweep_bps",
        }:
            raise StudyConfigError("configuration requires swing, zone and min_sweep_bps settings")
        zone_config = SupportResistanceZonesStudy().validate_config(
            {key: value for key, value in raw.items() if key != "min_sweep_bps"}
        )
        threshold = raw["min_sweep_bps"]
        if not isinstance(threshold, (int, float)) or type(threshold) not in (int, float):
            raise StudyConfigError("min_sweep_bps must be a finite positive built-in number")
        try:
            number = float(threshold)
        except OverflowError as error:
            raise StudyConfigError("min_sweep_bps must be a finite positive number") from error
        if not math.isfinite(number) or number <= 0:
            raise StudyConfigError("min_sweep_bps must be a finite positive built-in number")
        return {**zone_config, "min_sweep_bps": number}

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        normalized = self.validate_config(config)
        return (
            SupportResistanceZonesStudy().min_bars(
                {key: value for key, value in normalized.items() if key != "min_sweep_bps"}
            )
            + 1
        )

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        raise RuntimeError("structure.liquidity_sweep requires StudyRunner computation context")

    def compute_with_context(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, ConfigValue],
        context: StudyComputationContext,
    ) -> StudyOutput:
        normalized = self.validate_config(config)
        zone_config = {key: value for key, value in normalized.items() if key != "min_sweep_bps"}
        lookback = normalized["lookback_bars"]
        threshold = normalized["min_sweep_bps"]
        assert type(lookback) is int and type(threshold) is float
        swing_output = ConfirmedSwingsStudy().compute_with_context(
            bars,
            {"left_bars": normalized["left_bars"], "right_bars": normalized["right_bars"]},
            context,
        )
        if any(type(event) is not StudyEvent for event in swing_output.events):
            raise ValueError("confirmed swing study returned a non-swing event")
        by_pivot: dict[datetime, list[StudyEvent]] = {}
        for event in swing_output.events:
            if isinstance(event, StudyEvent):
                by_pivot.setdefault(event.pivot_time_utc, []).append(event)
        zone_study = SupportResistanceZonesStudy()
        zone_minimum = zone_study.min_bars(zone_config)
        events: list[StructureSweepEvent] = []
        for index in range(zone_minimum, len(bars)):
            previous, bar = bars[index - 1], bars[index]
            history = bars[max(0, index - lookback) : index]
            candidates = tuple(
                swing
                for item in history
                for swing in by_pivot.get(item.timestamp_utc, [])
                if swing.known_at_utc <= bar.timestamp_utc
            )
            zones = zone_study._zones_from_swings(
                history, candidates, zone_config, bar.timestamp_utc, context.calendar_context_id
            )
            completion = bar_completion_utc(
                bar.timestamp_utc, context.timeframe, timezone_name=context.calendar_timezone
            )
            if completion > context.as_of_utc:
                raise ValueError("sweep candle completion is later than the analysis cutoff")
            for side, zone_type in (
                (StructureSweepSide.HIGH_SIDE, StructureZoneType.RESISTANCE),
                (StructureSweepSide.LOW_SIDE, StructureZoneType.SUPPORT),
            ):
                eligible: list[StructureZone] = []
                for zone in zones:
                    if zone.zone_type is not zone_type or zone.known_at_utc > bar.timestamp_utc:
                        continue
                    level = (
                        zone.upper_price
                        if side is StructureSweepSide.HIGH_SIDE
                        else zone.lower_price
                    )
                    wick = bar.high if side is StructureSweepSide.HIGH_SIDE else bar.low
                    if side is StructureSweepSide.HIGH_SIDE:
                        valid = previous.close <= level < wick and bar.close <= level
                        overshoot = Fraction(str(wick)) - Fraction(str(level))
                    else:
                        valid = previous.close >= level > wick and bar.close >= level
                        overshoot = Fraction(str(level)) - Fraction(str(wick))
                    if valid and overshoot * 10000 >= Fraction(str(level)) * Fraction(
                        str(threshold)
                    ):
                        eligible.append(zone)
                if not eligible:
                    continue
                nearest = (
                    min(zone.upper_price for zone in eligible)
                    if side is StructureSweepSide.HIGH_SIDE
                    else max(zone.lower_price for zone in eligible)
                )
                chosen = max(
                    (
                        zone
                        for zone in eligible
                        if (
                            zone.upper_price
                            if side is StructureSweepSide.HIGH_SIDE
                            else zone.lower_price
                        )
                        == nearest
                    ),
                    key=lambda zone: (zone.known_at_utc, zone.event_id),
                )
                wick = bar.high if side is StructureSweepSide.HIGH_SIDE else bar.low
                events.append(
                    StructureSweepEvent(
                        structure_sweep_event_id(
                            self.study_id,
                            self.version,
                            bar.instrument,
                            bar.timeframe,
                            chosen.event_id,
                            bar.timestamp_utc,
                            threshold,
                        ),
                        self.study_id,
                        self.version,
                        bar.instrument,
                        bar.timeframe,
                        chosen,
                        side,
                        bar.timestamp_utc,
                        completion,
                        previous.close,
                        wick,
                        bar.close,
                        threshold,
                    )
                )
        events.sort(
            key=lambda event: (
                event.known_at_utc,
                event.sweep_bar_time_utc,
                event.side.value,
                event.event_id,
            )
        )
        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state="SWEEPS_IDENTIFIED" if events else "NO_SWEEPS",
            direction=Direction.NOT_APPLICABLE,
            evidence=(f"event_count={len(events)}",),
            limitations=(
                "Wick reclaims are descriptive, not predictive signals or trading approval.",
                "Volume-based strength is unavailable and unimplemented.",
                "High-side and low-side events do not imply an intrabar sequence.",
            ),
            events=tuple(events),
        )
