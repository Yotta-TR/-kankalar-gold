"""Independent synthetic examples and runner integration for Phase 2B studies."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from kankalar_gold_ai.core.contracts import Direction, MarketBar, StudyResult, StudyStatus
from kankalar_gold_ai.core.study_results import StudyResultKey, canonical_inputs_json
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.data.snapshot import compute_bar_snapshot_id
from kankalar_gold_ai.data.validated import ValidatedBarSet
from kankalar_gold_ai.storage.sqlite import SQLitePersistence
from kankalar_gold_ai.studies import (
    StudyConfigError,
    StudyRegistry,
    StudyRunner,
    StudyRunRequest,
    run_pipeline_study,
)
from kankalar_gold_ai.studies.base import BarStudy, ConfigValue, StudyOutput
from kankalar_gold_ai.studies.donchian import DonchianStudy
from kankalar_gold_ai.studies.macd import MACDStudy
from tests.fixtures.point_in_time import (
    DURATIONS,
    assert_no_lookahead,
    blind,
    build_universe,
    bump_last_selected_close,
    end_of,
)
from tests.test_rsi_roc import CUTOFF, bars

MACD = {"fast_period": 2, "slow_period": 3, "signal_period": 2}
DONCHIAN = {"donchian_period": 2}
CASES = [(MACDStudy, MACD), (DonchianStudy, DONCHIAN)]


def channel_bars(close: float, *, high: float | None = None) -> tuple[MarketBar, ...]:
    items = bars((10.0, 11.0, close))
    return (
        replace(items[0], high=12.0, low=8.0),
        replace(items[1], high=13.0, low=9.0),
        replace(items[2], high=max(close, high or close), low=min(close, 10.0)),
    )


def setup(tmp_path: Path, study: BarStudy) -> tuple[StudyRunner, SQLitePersistence]:
    registry = StudyRegistry()
    registered = registry.register(study)
    assert registry.get(study.study_id, "0.1.0") is registered
    store = SQLitePersistence(tmp_path / "studies.db")
    store.initialize()
    return StudyRunner(registry, store), store


def run(
    runner: StudyRunner, study: BarStudy, items: tuple[MarketBar, ...], config: dict[str, int]
) -> StudyResult:
    timeframe = items[0].timeframe
    request = StudyRunRequest(study.study_id, study.version, "TEST_GOLD", timeframe, CUTOFF, config)
    validated = ValidatedBarSet.from_bars(items, timeframe, CUTOFF, cutoff_utc=CUTOFF)
    return runner.run(request, validated)


def evidence(output: StudyResult | StudyOutput) -> dict[str, str]:
    return dict(item.split("=", 1) for item in output.evidence)


@pytest.mark.parametrize(
    (
        "closes",
        "histogram",
        "line",
        "signal",
        "previous",
        "change",
        "state",
        "direction",
        "relation",
    ),
    [
        (
            (1.0, 2.0, 3.0, 4.0, 6.0),
            1 / 18,
            1 / 6 + 0.5,
            11 / 18,
            0.0,
            1 / 18,
            "HISTOGRAM_POSITIVE",
            Direction.BULLISH,
            "positive",
        ),
        (
            (1.0, 2.0, 3.0, 4.0, 2.0),
            -1 / 6,
            0.0,
            1 / 6,
            0.0,
            -1 / 6,
            "HISTOGRAM_NEGATIVE",
            Direction.BEARISH,
            "zero",
        ),
        (
            (1.0, 2.0, 3.0, 4.0, 6.0, 6.0),
            -5 / 108,
            17 / 36,
            14 / 27,
            1 / 18,
            -11 / 108,
            "HISTOGRAM_NEGATIVE",
            Direction.BEARISH,
            "positive",
        ),
        (
            (6.0, 5.0, 4.0, 3.0, 2.0),
            0.0,
            -0.5,
            -0.5,
            0.0,
            0.0,
            "HISTOGRAM_ZERO",
            Direction.NEUTRAL,
            "negative",
        ),
        (
            (5.0, 5.0, 5.0, 5.0, 5.0),
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            "HISTOGRAM_ZERO",
            Direction.NEUTRAL,
            "zero",
        ),
    ],
)
def test_macd_hand_calculated(
    closes: tuple[float, ...],
    histogram: float,
    line: float,
    signal: float,
    previous: float,
    change: float,
    state: str,
    direction: Direction,
    relation: str,
) -> None:
    output = MACDStudy().compute(bars(closes), MACD)
    audit = evidence(output)
    assert output.value == pytest.approx(histogram)
    assert output.normalized_value is None
    assert (output.state, output.direction) == (state, direction)
    for key, expected in {
        "macd_line": line,
        "signal_line": signal,
        "histogram": histogram,
        "previous_histogram": previous,
        "histogram_change": change,
    }.items():
        assert float(audit[key]) == pytest.approx(expected)
    assert audit["macd_zero_line_relation"] == relation
    assert {key: int(audit[key]) for key in MACD} == MACD


def test_macd_signal_period_one_is_exactly_zero() -> None:
    output = MACDStudy().compute(bars((1.0, 3.0, 2.0, 5.0)), {**MACD, "signal_period": 1})
    assert output.value == 0.0
    assert output.state == "HISTOGRAM_ZERO" and output.direction is Direction.NEUTRAL
    assert float(evidence(output)["previous_histogram"]) == 0.0


@pytest.mark.parametrize(
    ("close", "position", "state", "direction", "comparison"),
    [
        (14.0, 1.2, "BREAKOUT_UP", Direction.BULLISH, "above_upper"),
        (7.0, -0.2, "BREAKOUT_DOWN", Direction.BEARISH, "below_lower"),
        (13.0, 1.0, "INSIDE_CHANNEL", Direction.NEUTRAL, "within_or_equal_boundaries"),
        (8.0, 0.0, "INSIDE_CHANNEL", Direction.NEUTRAL, "within_or_equal_boundaries"),
        (10.0, 0.4, "INSIDE_CHANNEL", Direction.NEUTRAL, "within_or_equal_boundaries"),
    ],
)
def test_donchian_hand_calculated(
    close: float, position: float, state: str, direction: Direction, comparison: str
) -> None:
    output = DonchianStudy().compute(channel_bars(close, high=20.0), DONCHIAN)
    assert output.value == pytest.approx(position)
    assert output.normalized_value is None
    assert (output.state, output.direction) == (state, direction)
    assert evidence(output) == {
        "donchian_period": "2",
        "upper_channel": "13.0",
        "lower_channel": "8.0",
        "channel_width": "5.0",
        "current_close": repr(close),
        "strict_comparison": comparison,
    }


@pytest.mark.parametrize(
    ("close", "state", "direction"),
    [
        (10.0, "ZERO_WIDTH_CHANNEL", Direction.NEUTRAL),
        (11.0, "BREAKOUT_UP", Direction.BULLISH),
        (9.0, "BREAKOUT_DOWN", Direction.BEARISH),
    ],
)
def test_donchian_zero_width(close: float, state: str, direction: Direction) -> None:
    output = DonchianStudy().compute(bars((10.0, 10.0, close)), DONCHIAN)
    assert output.value is None
    assert (output.state, output.direction) == (state, direction)


@pytest.mark.parametrize(("factory", "config"), CASES)
@pytest.mark.parametrize("bad", [True, False, 2.0, "2", None, 0, -1])
def test_invalid_config(
    factory: type[MACDStudy] | type[DonchianStudy], config: dict[str, int], bad: ConfigValue
) -> None:
    study = factory()
    for key in config:
        with pytest.raises(StudyConfigError):
            study.validate_config({**config, key: bad})
        with pytest.raises(StudyConfigError):
            study.validate_config({k: v for k, v in config.items() if k != key})
    with pytest.raises(StudyConfigError):
        study.validate_config({**config, "extra": 1})


@pytest.mark.parametrize(("fast", "slow"), [(2, 2), (3, 2)])
def test_macd_requires_fast_less_than_slow(fast: int, slow: int) -> None:
    with pytest.raises(StudyConfigError):
        MACDStudy().validate_config({**MACD, "fast_period": fast, "slow_period": slow})


@pytest.mark.parametrize(("factory", "config"), CASES)
def test_warmup_boundary(
    tmp_path: Path, factory: type[MACDStudy] | type[DonchianStudy], config: dict[str, int]
) -> None:
    study = factory()
    runner, _ = setup(tmp_path, study)
    minimum = 5 if factory is MACDStudy else 3
    assert study.min_bars(config) == minimum
    short = run(runner, study, bars(tuple(float(i + 10) for i in range(minimum - 1))), config)
    exact = run(runner, study, bars(tuple(float(i + 10) for i in range(minimum))), config)
    assert short.status is StudyStatus.INSUFFICIENT_DATA and short.value is None
    assert exact.status is StudyStatus.AVAILABLE


@pytest.mark.parametrize(("factory", "config"), CASES)
@pytest.mark.parametrize("timeframe", tuple(DURATIONS))
def test_registry_snapshot_cache_roundtrip(
    tmp_path: Path,
    factory: type[MACDStudy] | type[DonchianStudy],
    config: dict[str, int],
    timeframe: str,
) -> None:
    study = factory()
    runner, store = setup(tmp_path, study)
    items = bars((10.0, 12.0, 11.0, 14.0, 13.0, 16.0, 12.0), timeframe)
    result = run(runner, study, items, config)
    assert result.status is StudyStatus.AVAILABLE
    assert result.version == "0.1.0" and result.inputs == config
    assert result.data_snapshot_id == compute_bar_snapshot_id(items)
    assert run(runner, study, items, config) == result
    key = StudyResultKey(
        result.study_id,
        result.version,
        result.instrument,
        timeframe,
        CUTOFF,
        canonical_inputs_json(result.inputs),
        result.data_snapshot_id,
        calendar_context_id=dict(result.provenance)["calendar_context_id"],
    )
    assert store.find_cached_study_result(key) == result
    assert SQLitePersistence(tmp_path / "studies.db").find_cached_study_result(key) == result
    variants = (
        [{**config, "fast_period": 1}, {**config, "slow_period": 4}, {**config, "signal_period": 3}]
        if factory is MACDStudy
        else [{**config, "donchian_period": 3}]
    )
    for variant in variants:
        other = run(runner, study, items, variant)
        assert other.inputs == variant and other.data_snapshot_id == result.data_snapshot_id
        other_key = replace(key, inputs_json=canonical_inputs_json(other.inputs))
        assert store.find_cached_study_result(other_key) == other
        assert other != result
    assert store.find_cached_study_result(replace(key, version="0.1.1")) is None
    changed = run(
        runner, study, bars((10.0, 12.0, 11.0, 14.0, 13.0, 16.0, 15.0), timeframe), config
    )
    assert changed.data_snapshot_id != result.data_snapshot_id


@pytest.mark.parametrize(("factory", "config"), CASES)
def test_pipeline_integration(
    tmp_path: Path, factory: type[MACDStudy] | type[DonchianStudy], config: dict[str, int]
) -> None:
    study = factory()
    runner, _ = setup(tmp_path, study)
    items = bars((10.0, 12.0, 11.0, 14.0, 13.0))

    class Provider:
        def get_bars(
            self, instrument: str, timeframe: str, start_utc: datetime, end_utc: datetime
        ) -> tuple[MarketBar, ...]:
            return items

    request = StudyRunRequest(study.study_id, study.version, "TEST_GOLD", "M5", CUTOFF, config)
    result = run_pipeline_study(
        runner, HistoricalDataPipeline(Provider(), clock=lambda: CUTOFF), request, history_days=1
    )
    assert result.status is StudyStatus.AVAILABLE
    assert result.data_snapshot_id == compute_bar_snapshot_id(items)


@pytest.mark.parametrize(("factory", "config"), CASES)
def test_no_lookahead(
    factory: type[MACDStudy] | type[DonchianStudy], config: dict[str, int]
) -> None:
    report = assert_no_lookahead(
        blind(factory),
        build_universe(18),
        "M5",
        (end_of(5), end_of(10), end_of(15)),
        config=config,
        sensitive_mutation=bump_last_selected_close("M5"),
    )
    assert report.cutoffs_checked == 3


@pytest.mark.parametrize("invalid", [0.0, -1.0, math.nan, math.inf, -math.inf])
def test_invalid_prices(invalid: float) -> None:
    with pytest.raises(ArithmeticError):
        MACDStudy().compute(bars((1.0, 2.0, 3.0, 4.0, invalid)), MACD)
    with pytest.raises(ArithmeticError):
        DonchianStudy().compute(bars((1.0, 2.0, invalid)), DONCHIAN)
    with pytest.raises(ArithmeticError):
        DonchianStudy().compute(bars((invalid, 2.0, 3.0)), DONCHIAN)


@pytest.mark.parametrize(("field", "invalid"), [("high", math.inf), ("low", math.nan)])
def test_donchian_rejects_nonfinite_previous_bounds(field: str, invalid: float) -> None:
    items = channel_bars(10.0)
    invalid_bar = replace(items[0], **{field: invalid})
    with pytest.raises(ArithmeticError):
        DonchianStudy().compute((invalid_bar, *items[1:]), DONCHIAN)


@pytest.mark.parametrize(
    ("study", "config", "closes"),
    [
        (MACDStudy(), MACD, (1e308, 1e308, 1e308, 1e308, 1e308)),
        (DonchianStudy(), DONCHIAN, (1e-320, 2e-320, 1e308)),
    ],
)
def test_arithmetic_overflow_persisted(
    tmp_path: Path, study: BarStudy, config: dict[str, int], closes: tuple[float, ...]
) -> None:
    runner, _ = setup(tmp_path, study)
    result = run(runner, study, bars(closes), config)
    assert result.status is StudyStatus.ERROR
    assert result.value is None and result.normalized_value is None and result.uncertainty is None
    assert result.direction is Direction.NOT_APPLICABLE
    with sqlite3.connect(tmp_path / "studies.db") as connection:
        row = connection.execute(
            "SELECT status, value, normalized_value, uncertainty, direction FROM study_results"
        ).fetchone()
    assert row == ("ERROR", None, None, None, "NOT_APPLICABLE")
