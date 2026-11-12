"""Small append-oriented SQLite store for Phase 0 research records."""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from kankalar_gold_ai.core.contracts import (
    MarketBar,
    SignalLifecycleEvent,
    StudyResult,
)


class SQLitePersistence:
    """Persist core records locally; never opens a broker or network connection."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)

    def initialize(self) -> None:
        """Create the Phase 0 schema without deleting or rewriting existing data."""
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_bars (
                    instrument TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL,
                    source TEXT NOT NULL,
                    PRIMARY KEY (instrument, timeframe, timestamp_utc, source)
                );
                CREATE TABLE IF NOT EXISTS study_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    study_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    as_of_utc TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state TEXT,
                    value REAL,
                    normalized_value REAL,
                    direction TEXT NOT NULL,
                    uncertainty REAL,
                    inputs_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    limitations_json TEXT NOT NULL,
                    data_snapshot_id TEXT
                );
                CREATE TABLE IF NOT EXISTS signal_events (
                    event_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    occurred_at_utc TEXT NOT NULL,
                    reason_code TEXT NOT NULL
                );
                """
            )

    def save_market_bars(self, bars: tuple[MarketBar, ...]) -> None:
        """Append bars; duplicate source observations raise an integrity error."""
        rows = (
            (
                bar.instrument,
                bar.timeframe,
                _utc_isoformat(bar.timestamp_utc),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.source,
            )
            for bar in bars
        )
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO market_bars
                (instrument, timeframe, timestamp_utc, open, high, low, close, volume, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def save_study_result(self, result: StudyResult) -> None:
        """Append one complete study result, preserving evidence and limitations."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO study_results (
                    study_id, version, instrument, timeframe, as_of_utc, status, state,
                    value, normalized_value, direction, uncertainty, inputs_json,
                    evidence_json, limitations_json, data_snapshot_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.study_id,
                    result.version,
                    result.instrument,
                    result.timeframe,
                    _utc_isoformat(result.as_of_utc),
                    result.status.value,
                    result.state,
                    result.value,
                    result.normalized_value,
                    result.direction.value,
                    result.uncertainty,
                    json.dumps(result.inputs, sort_keys=True),
                    json.dumps(result.evidence),
                    json.dumps(result.limitations),
                    result.data_snapshot_id,
                ),
            )

    def save_signal_event(self, event: SignalLifecycleEvent) -> None:
        """Append one lifecycle event; duplicate event ids are rejected."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO signal_events
                (event_id, signal_id, state, occurred_at_utc, reason_code)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.signal_id,
                    event.state.value,
                    _utc_isoformat(event.occurred_at_utc),
                    event.reason_code,
                ),
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path)) as connection:
            with connection:
                yield connection


def _utc_isoformat(value: datetime) -> str:
    """Serialize a contract timestamp as an unambiguous UTC ISO-8601 string."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
