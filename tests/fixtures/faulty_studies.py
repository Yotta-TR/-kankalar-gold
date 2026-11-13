"""Test-only canary and deliberately faulty studies for the F4 no-lookahead harness.

All environment dependencies are scripted: no wall clock, no uncontrolled randomness.
Only ``make_future_peeker`` receives the full historical universe; ordinary studies never do.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from kankalar_gold_ai.core.contracts import MarketBar, StudyStatus
from kankalar_gold_ai.studies import StudyOutput
from kankalar_gold_ai.studies.base import ConfigValue
from tests.fixtures.point_in_time import DURATIONS


def mean_last_three(bars: tuple[MarketBar, ...]) -> float:
    return sum(bar.close for bar in bars[-3:]) / 3


class CausalMean3:
    """Causal canary: mean of the last three closes. Sensitive only to those three bars."""

    study_id = "trend.f4_canary_mean3"
    version = "1.0.0"

    def __init__(self, timeframe: str = "M5") -> None:
        self.required_timeframes = frozenset({timeframe})
        self.calls = 0
        self.seen_bars: list[tuple[MarketBar, ...]] = []

    def validate_config(self, raw: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
        return dict(raw)

    def min_bars(self, config: Mapping[str, ConfigValue]) -> int:
        return 3

    def value(self, bars: tuple[MarketBar, ...]) -> float:
        return mean_last_three(bars)

    def compute(
        self, bars: tuple[MarketBar, ...], config: Mapping[str, ConfigValue]
    ) -> StudyOutput:
        self.calls += 1
        self.seen_bars.append(bars)
        return StudyOutput(
            status=StudyStatus.AVAILABLE, value=self.value(bars), evidence=("MEAN3",)
        )


class ConstantStudy(CausalMean3):
    """Insensitive to every mutation; used only to prove the harness guards fire."""

    study_id = "trend.f4_constant"

    def value(self, bars: tuple[MarketBar, ...]) -> float:
        return 1.0


class FuturePeekingStudy(CausalMean3):
    """Leaky control: reads the bar after the window from a closed-over full universe."""

    study_id = "trend.f4_future_peeker"

    def __init__(self, universe: tuple[MarketBar, ...], timeframe: str = "M5") -> None:
        super().__init__(timeframe)
        self._universe = universe
        self._duration = DURATIONS[timeframe]

    def value(self, bars: tuple[MarketBar, ...]) -> float:
        base = mean_last_three(bars)
        following = bars[-1].timestamp_utc + self._duration
        for bar in self._universe:
            if bar.timestamp_utc == following:
                return (base + bar.close) / 2
        return base


def make_future_peeker(universe: tuple[MarketBar, ...]) -> FuturePeekingStudy:
    return FuturePeekingStudy(universe)


class RepaintingStudy(CausalMean3):
    """Stateful control: the result depends on how many evaluations preceded it."""

    study_id = "trend.f4_repainter"

    def value(self, bars: tuple[MarketBar, ...]) -> float:
        # compute() increments calls before value(); the first evaluation adds zero.
        return mean_last_three(bars) + 0.001 * (self.calls - 1)


class ClockStudy(CausalMean3):
    """Control reading an injected scripted clock (seconds), shared across instances."""

    study_id = "trend.f4_clock"

    def __init__(self, clock: Callable[[], float], timeframe: str = "M5") -> None:
        super().__init__(timeframe)
        self._clock = clock

    def value(self, bars: tuple[MarketBar, ...]) -> float:
        return mean_last_three(bars) + self._clock()


class RandomStudy(CausalMean3):
    """Control reading an injected scripted random source, shared across instances."""

    study_id = "trend.f4_random"

    def __init__(self, draw: Callable[[], float], timeframe: str = "M5") -> None:
        super().__init__(timeframe)
        self._draw = draw

    def value(self, bars: tuple[MarketBar, ...]) -> float:
        return mean_last_three(bars) + self._draw()
