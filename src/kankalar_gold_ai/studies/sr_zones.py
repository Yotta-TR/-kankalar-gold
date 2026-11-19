"""Descriptive complete-linkage zones from confirmed swing events."""

import math
from collections.abc import Mapping
from datetime import datetime
from fractions import Fraction

from kankalar_gold_ai.core.contracts import (
    Direction,
    MarketBar,
    StructureZone,
    StructureZoneType,
    StudyEvent,
    StudyEventType,
    StudyStatus,
    structure_zone_id,
)
from kankalar_gold_ai.studies.base import ConfigValue, StudyComputationContext, StudyOutput
from kankalar_gold_ai.studies.errors import StudyConfigError
from kankalar_gold_ai.studies.swings import ConfirmedSwingsStudy


class SupportResistanceZonesStudy:
    study_id = "structure.sr_zones"
    version = "0.1.0"
    required_timeframes = ConfirmedSwingsStudy.required_timeframes

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        if set(raw) != {"left_bars", "right_bars", "lookback_bars", "tolerance_bps"}:
            raise StudyConfigError(
                "configuration requires left_bars, right_bars, lookback_bars and tolerance_bps"
            )
        swing_config = ConfirmedSwingsStudy().validate_config(
            {"left_bars": raw["left_bars"], "right_bars": raw["right_bars"]}
        )
        lookback = raw["lookback_bars"]
        tolerance = raw["tolerance_bps"]
        if type(lookback) is not int or lookback <= 0:
            raise StudyConfigError("lookback_bars must be a positive built-in integer")
        if not isinstance(tolerance, (int, float)) or type(tolerance) not in (int, float):
            raise StudyConfigError("tolerance_bps must be a finite positive built-in number")
        try:
            tolerance_float = float(tolerance)
        except OverflowError as error:
            raise StudyConfigError("tolerance_bps must be a finite positive number") from error
        if not math.isfinite(tolerance_float) or tolerance_float <= 0:
            raise StudyConfigError("tolerance_bps must be a finite positive built-in number")
        return {**swing_config, "lookback_bars": lookback, "tolerance_bps": tolerance_float}

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        normalized = self.validate_config(config)
        lookback = normalized["lookback_bars"]
        assert type(lookback) is int
        return max(
            lookback,
            ConfirmedSwingsStudy().min_bars(
                {
                    "left_bars": normalized["left_bars"],
                    "right_bars": normalized["right_bars"],
                }
            ),
        )

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        raise RuntimeError("structure.sr_zones requires StudyRunner computation context")

    def compute_with_context(
        self,
        bars: tuple[MarketBar, ...],
        config: Mapping[str, ConfigValue],
        context: StudyComputationContext,
    ) -> StudyOutput:
        normalized = self.validate_config(config)
        output = ConfirmedSwingsStudy().compute_with_context(
            bars,
            {"left_bars": normalized["left_bars"], "right_bars": normalized["right_bars"]},
            context,
        )
        if any(type(event) is not StudyEvent for event in output.events):
            raise ValueError("confirmed swing study returned a non-swing event")
        swings = tuple(event for event in output.events if isinstance(event, StudyEvent))
        zones = self._zones_from_swings(
            bars, swings, normalized, context.as_of_utc, context.calendar_context_id
        )
        return StudyOutput(
            status=StudyStatus.AVAILABLE,
            state="ZONES_IDENTIFIED" if zones else "NO_ZONES",
            direction=Direction.NOT_APPLICABLE,
            evidence=(f"zone_count={len(zones)}",),
            limitations=(
                "Zones are descriptive, not signals or trading approval.",
                "Volume-based zone strength is unavailable and unimplemented.",
            ),
            events=zones,
        )

    def _zones_from_swings(
        self,
        bars: tuple[MarketBar, ...],
        confirmed_swings: tuple[StudyEvent, ...],
        config: Mapping[str, ConfigValue],
        as_of_utc: datetime,
        calendar_context_id: str,
    ) -> tuple[StructureZone, ...]:
        """Build the same cutoff-bound zones from already computed confirmed swings."""
        normalized = self.validate_config(config)
        lookback, tolerance = normalized["lookback_bars"], normalized["tolerance_bps"]
        assert type(lookback) is int and type(tolerance) is float
        if not bars:
            raise ValueError("zone construction requires bars")
        eligible_pivots = {bar.timestamp_utc for bar in bars[-lookback:]}
        zones: list[StructureZone] = []
        for swing_type, zone_type in (
            (StudyEventType.SWING_LOW, StructureZoneType.SUPPORT),
            (StudyEventType.SWING_HIGH, StructureZoneType.RESISTANCE),
        ):
            swings = sorted(
                (
                    event
                    for event in confirmed_swings
                    if event.event_type is swing_type
                    and event.pivot_time_utc in eligible_pivots
                    and event.known_at_utc <= as_of_utc
                ),
                key=lambda event: (event.price, event.event_id),
            )
            clusters: list[list[StudyEvent]] = []
            for swing in swings:
                if not clusters or (
                    (Fraction(str(swing.price)) - Fraction(str(clusters[-1][0].price))) * 10000
                    > Fraction(str(clusters[-1][0].price)) * Fraction(str(tolerance))
                ):
                    clusters.append([swing])
                else:
                    clusters[-1].append(swing)
            for cluster in clusters:
                if len(cluster) < 2:
                    continue
                member_ids = tuple(swing.event_id for swing in cluster)
                prices = tuple(swing.price for swing in cluster)
                known_at = max(swing.known_at_utc for swing in cluster)
                identity = structure_zone_id(
                    self.study_id,
                    self.version,
                    bars[0].instrument,
                    bars[0].timeframe,
                    zone_type,
                    member_ids,
                    prices,
                    known_at,
                    lookback,
                    tolerance,
                    calendar_context_id,
                )
                zones.append(
                    StructureZone(
                        identity,
                        self.study_id,
                        self.version,
                        bars[0].instrument,
                        bars[0].timeframe,
                        zone_type,
                        member_ids,
                        prices,
                        prices[0],
                        prices[-1],
                        known_at,
                        lookback,
                        tolerance,
                        calendar_context_id,
                    )
                )
        zones.sort(key=lambda zone: (zone.known_at_utc, zone.zone_type.value, zone.event_id))
        return tuple(zones)
