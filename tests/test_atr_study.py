"""ATR study unit tests using deterministic synthetic M15 OHLC bars."""

import math
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from kankalar_gold_ai.core.contracts import Direction, MarketBar, StudyStatus
from kankalar_gold_ai.data.errors import HistoricalDataQualityError
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.studies.atr import ATRStudy
from kankalar_gold_ai.studies.base import ConfigValue
from kankalar_gold_ai.studies.errors import StudyConfigError

START = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
DURATION = timedelta(minutes=15)
SOURCE = "synthetic-atr-fixture"


def _bar(
    index: int,
    *,
    high: float,
    low: float,
    close: float,
    open_price: float | None = None,
    timestamp: datetime | None = None,
) -> MarketBar:
    return MarketBar(
        instrument="TEST_GOLD",
        timeframe="M15",
        timestamp_utc=timestamp or START + DURATION * index,
        open=close if open_price is None else open_price,
        high=high,
        low=low,
        close=close,
        volume=None,
        source=SOURCE,
    )


def _range_bars() -> tuple[MarketBar, ...]:
    return (
        _bar(0, high=100.5, low=99.5, close=100.0),
        _bar(1, high=101.0, low=99.0, close=100.0),
        _bar(2, high=102.0, low=98.0, close=100.0),
        _bar(3, high=103.0, low=97.0, close=100.0),
    )


def _validated(
    bars: tuple[MarketBar, ...],
    *,
    expected_open: Callable[[datetime], bool] | None = None,
) -> ValidatedBarSet:
    cutoff = bars[-1].timestamp_utc + DURATION
    return ValidatedBarSet.from_bars(
        bars,
        "M15",
        cutoff,
        cutoff_utc=cutoff,
        expected_open=expected_open,
    )


def test_omits_first_bar_and_uses_previous_close_for_gap_up_and_gap_down() -> None:
    bars = (
        _bar(0, high=103.0, low=99.0, close=100.0),
        _bar(1, high=108.0, low=105.0, close=107.0, open_price=106.0),
        _bar(2, high=99.0, low=95.0, close=96.0, open_price=98.0),
    )
    study = ATRStudy()

    up = study.compute(bars[:2], {"atr_period": 1})
    down = study.compute(bars[1:], {"atr_period": 1})
    both = study.compute(bars, {"atr_period": 2})

    assert up.value == 8.0
    assert down.value == 12.0
    assert both.value == 10.0


def test_wilder_seed_and_recurrence_are_exact() -> None:
    study = ATRStudy()
    bars = _range_bars()

    seeded = study.compute(bars[:3], {"atr_period": 2})
    recurrent = study.compute(bars, {"atr_period": 2})

    assert seeded.value == 3.0
    assert recurrent.value == 4.5
    assert recurrent.value is not None
    assert math.isfinite(recurrent.value) and recurrent.value >= 0
    assert study.compute(bars, {"atr_period": 2}) == recurrent


def test_configuration_is_explicit_positive_builtin_integer_only() -> None:
    study = ATRStudy()

    assert study.required_timeframes == frozenset({"M1", "M5", "M15", "H1", "H4", "D1"})
    assert study.validate_config({"atr_period": 14}) == {"atr_period": 14}
    assert study.min_bars({"atr_period": 14}) == 15
    assert study.validate_config({"atr_period": 1_000_000}) == {"atr_period": 1_000_000}
    invalid: tuple[dict[str, ConfigValue], ...] = (
        {},
        {"atr_period": 14, "other": 1},
        {"atr_period": True},
        {"atr_period": 1.0},
        {"atr_period": "1"},
        {"atr_period": None},
        {"atr_period": 0},
        {"atr_period": -1},
    )
    for config in invalid:
        with pytest.raises(StudyConfigError):
            study.validate_config(config)


def test_output_is_descriptive_and_zero_is_preserved() -> None:
    bars = tuple(_bar(i, high=100.0, low=100.0, close=100.0) for i in range(2))
    result = ATRStudy().compute(bars, {"atr_period": 1})

    assert result.status is StudyStatus.AVAILABLE
    assert result.value == 0.0
    assert result.value is not None and result.value >= 0
    assert result.normalized_value is None
    assert result.state is None
    assert result.direction is Direction.NOT_APPLICABLE
    assert result.uncertainty is None
    assert result.evidence
    assert any("no True Range observation" in item for item in result.evidence)
    assert result.limitations
    assert "direction" in result.limitations[0]


def test_known_closure_uses_previous_observed_close_and_unknown_gap_fails_closed() -> None:
    bars = (
        _bar(0, high=101.0, low=99.0, close=100.0),
        _bar(
            1,
            high=106.0,
            low=104.0,
            close=105.0,
            timestamp=START + DURATION * 2,
        ),
    )

    with pytest.raises(HistoricalDataQualityError):
        _validated(bars)

    validated = _validated(bars, expected_open=lambda _timestamp: False)
    result = ATRStudy().compute(validated.bars, {"atr_period": 1})
    assert validated.expected_open is not None
    assert result.value == 6.0


def test_validated_input_rejects_non_finite_ohlc_instead_of_zeroing_it() -> None:
    invalid = (
        _bar(0, high=float("nan"), low=99.0, close=100.0),
        _bar(1, high=101.0, low=99.0, close=100.0),
    )

    with pytest.raises(HistoricalDataQualityError):
        _validated(invalid)
