"""Structure-break identity and immutable value contract."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from kankalar_gold_ai.core.contracts import (
    Direction,
    StructureBreakEvent,
    StructureBreakType,
    structure_break_event_id,
)


def _event() -> StructureBreakEvent:
    bar_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    context = "calendar-context-v1:sha256:" + "a" * 64
    identity = ("structure.bos_choch", "0.1.0", "GOLD", "M5", "swing-id", bar_time, context)
    return StructureBreakEvent(
        event_id=structure_break_event_id(*identity),
        study_id=identity[0],
        version=identity[1],
        instrument=identity[2],
        timeframe=identity[3],
        reference_swing_id=identity[4],
        broken_level=10,
        break_bar_time_utc=bar_time,
        known_at_utc=bar_time + timedelta(minutes=5),
        direction=Direction.BULLISH,
        classification=StructureBreakType.INITIAL_BREAK,
        calendar_context_id=context,
    )


def test_structure_break_is_immutable_and_has_stable_identity() -> None:
    event = _event()
    assert _event() == event
    assert type(event.broken_level) is float
    with pytest.raises(FrozenInstanceError):
        event.broken_level = 11  # type: ignore[misc]
    with pytest.raises(ValueError, match="canonical"):
        replace(event, reference_swing_id="different")
    other_context = "calendar-context-v1:sha256:" + "b" * 64
    assert event.event_id != structure_break_event_id(
        event.study_id,
        event.version,
        event.instrument,
        event.timeframe,
        event.reference_swing_id,
        event.break_bar_time_utc,
        other_context,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"direction": Direction.NEUTRAL},
        {"classification": "BOS"},
        {"broken_level": float("nan")},
        {"broken_level": True},
        {"calendar_context_id": "invalid"},
    ],
)
def test_structure_break_rejects_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(_event(), **changes)


def test_structure_break_requires_aware_completion_after_open() -> None:
    event = _event()
    with pytest.raises(ValueError, match="later"):
        replace(event, known_at_utc=event.break_bar_time_utc)
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(event, known_at_utc=datetime(2026, 1, 1))
