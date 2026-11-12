"""Safe, versioned SQLite persistence for Phase 0 research records and Phase 1A data."""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from kankalar_gold_ai.core.contracts import (
    InstrumentSpecification,
    MarketBar,
    MarketDataHealth,
    MarketQuote,
    MarketTick,
    SignalLifecycleEvent,
    StudyResult,
)


class MigrationError(RuntimeError):
    """A database migration failed."""


class SQLitePersistence:
    """Append-oriented SQLite persistence; never opens a broker or network connection."""

    SCHEMA_VERSION = 2

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._version: int | None = None

    def _get_version(self, connection: sqlite3.Connection) -> int:
        """Get the current schema version; default to 0 if not initialized."""
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        if table is None:
            return 0
        rows = connection.execute("SELECT version FROM schema_version").fetchall()
        if len(rows) > 1:
            raise MigrationError("schema_version contains multiple version records")
        if not rows:
            return 0
        version = rows[0][0]
        if not isinstance(version, int) or version < 0:
            raise MigrationError(f"Invalid SQLite schema version: {version!r}")
        return version

    def _set_version(self, connection: sqlite3.Connection, version: int) -> None:
        """Advance the schema version exactly one step without replacing it."""
        current = self._get_version(connection)
        if version != current + 1:
            raise MigrationError(
                f"Cannot advance SQLite schema version from {current} to {version}"
            )
        if current == 0:
            connection.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (version,),
            )
            return
        cursor = connection.execute(
            "UPDATE schema_version SET version = ? WHERE version = ?",
            (version, current),
        )
        if cursor.rowcount != 1:
            raise MigrationError("SQLite schema version changed during migration")

    def initialize(self) -> None:
        """
        Create or migrate the schema without deleting existing data.

        All migration steps and version updates commit or roll back together.
        """
        with self._connection() as connection:
            step = "starting transaction"
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = self._get_version(connection)
                if current > self.SCHEMA_VERSION:
                    raise MigrationError(
                        f"Database schema version {current} is newer than supported "
                        f"version {self.SCHEMA_VERSION}"
                    )

                migrations = {
                    1: self._migrate_to_version_1,
                    2: self._migrate_to_version_2,
                }
                while current < self.SCHEMA_VERSION:
                    target = current + 1
                    step = f"version {target}"
                    migrations[target](connection)
                    self._set_version(connection, target)
                    current = target
                connection.commit()
            except Exception as error:
                if connection.in_transaction:
                    connection.rollback()
                if isinstance(error, MigrationError):
                    raise
                raise MigrationError(
                    f"SQLite migration failed during {step}; all migration changes rolled back"
                ) from error

    def _migrate_to_version_1(self, connection: sqlite3.Connection) -> None:
        """Phase 0 schema: study results, signal events, and market bars."""
        statements = (
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
            """,
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
            )
            """,
            """
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
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS signal_events (
                event_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL,
                state TEXT NOT NULL,
                occurred_at_utc TEXT NOT NULL,
                reason_code TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_bars_instrument_timeframe
                ON market_bars(instrument, timeframe)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_study_results_study_id
                ON study_results(study_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_signal_events_signal_id
                ON signal_events(signal_id)
            """,
        )
        for statement in statements:
            connection.execute(statement)

    def _migrate_to_version_2(self, connection: sqlite3.Connection) -> None:
        """Phase 1A schema: instrument specs, ticks, quotes, and health events."""
        statements = (
            """
            CREATE TABLE IF NOT EXISTS instrument_specifications (
                symbol TEXT NOT NULL,
                source TEXT NOT NULL,
                retrieved_at_utc TEXT NOT NULL,
                digits INTEGER,
                point REAL,
                contract_size REAL,
                tick_size REAL,
                tick_value REAL,
                volume_min REAL,
                volume_max REAL,
                volume_step REAL,
                trade_mode INTEGER,
                stops_level INTEGER,
                freeze_level INTEGER,
                currency_profit TEXT,
                currency_margin TEXT,
                PRIMARY KEY (symbol, source, retrieved_at_utc)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS market_ticks (
                instrument TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                timestamp_milliseconds INTEGER,
                bid REAL,
                ask REAL,
                last REAL,
                volume REAL,
                flags INTEGER,
                source TEXT NOT NULL,
                retrieved_at_utc TEXT NOT NULL,
                PRIMARY KEY (instrument, timestamp_utc, source)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS market_quotes (
                instrument TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                timestamp_milliseconds INTEGER,
                bid REAL NOT NULL,
                ask REAL NOT NULL,
                last REAL,
                source TEXT NOT NULL,
                retrieved_at_utc TEXT NOT NULL,
                PRIMARY KEY (instrument, timestamp_utc, source)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS data_quality_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument TEXT NOT NULL,
                timeframe TEXT,
                timestamp_utc TEXT NOT NULL,
                status TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                source TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS provider_health_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                checked_at_utc TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL,
                terminal_version TEXT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_ticks_instrument
                ON market_ticks(instrument)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_quotes_instrument
                ON market_quotes(instrument)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_quality_events_instrument
                ON data_quality_events(instrument, timeframe)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_health_events_source
                ON provider_health_events(source)
            """,
        )
        for statement in statements:
            connection.execute(statement)

    def save_market_bars(self, bars: tuple[MarketBar, ...]) -> None:
        """Append bars; duplicate source observations raise an integrity error."""
        rows = [
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
        ]
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO market_bars
                (instrument, timeframe, timestamp_utc, open, high, low, close, volume, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def save_instrument_specification(self, spec: InstrumentSpecification) -> None:
        """Record an instrument specification snapshot."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO instrument_specifications
                (symbol, source, retrieved_at_utc, digits, point, contract_size, tick_size,
                 tick_value, volume_min, volume_max, volume_step, trade_mode, stops_level,
                 freeze_level, currency_profit, currency_margin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.symbol,
                    spec.source,
                    _utc_isoformat(spec.retrieved_at_utc),
                    spec.digits,
                    spec.point,
                    spec.contract_size,
                    spec.tick_size,
                    spec.tick_value,
                    spec.volume_min,
                    spec.volume_max,
                    spec.volume_step,
                    spec.trade_mode,
                    spec.stops_level,
                    spec.freeze_level,
                    spec.currency_profit,
                    spec.currency_margin,
                ),
            )

    def save_market_ticks(self, ticks: tuple[MarketTick, ...]) -> None:
        """Append market ticks; duplicates are rejected by primary key."""
        rows = [
            (
                tick.instrument,
                _utc_isoformat(tick.timestamp_utc),
                tick.timestamp_milliseconds,
                tick.bid,
                tick.ask,
                tick.last,
                tick.volume,
                tick.flags,
                tick.source,
                _utc_isoformat(tick.retrieved_at_utc),
            )
            for tick in ticks
        ]
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO market_ticks
                (instrument, timestamp_utc, timestamp_milliseconds, bid, ask, last, volume,
                 flags, source, retrieved_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def save_market_quote(self, quote: MarketQuote) -> None:
        """Record a market quote snapshot; duplicates are rejected by primary key."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO market_quotes
                (instrument, timestamp_utc, timestamp_milliseconds, bid, ask, last, source,
                 retrieved_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quote.instrument,
                    _utc_isoformat(quote.timestamp_utc),
                    quote.timestamp_milliseconds,
                    quote.bid,
                    quote.ask,
                    quote.last,
                    quote.source,
                    _utc_isoformat(quote.retrieved_at_utc),
                ),
            )

    def save_data_quality_event(
        self,
        instrument: str,
        timeframe: str | None,
        timestamp_utc: datetime,
        status: str,
        reasons: tuple[str, ...],
        source: str | None = None,
    ) -> None:
        """Record a data-quality check event."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO data_quality_events
                (instrument, timeframe, timestamp_utc, status, reasons_json, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    instrument,
                    timeframe,
                    _utc_isoformat(as_utc(timestamp_utc)),
                    status,
                    json.dumps(reasons),
                    source,
                ),
            )

    def save_provider_health_event(self, health: MarketDataHealth) -> None:
        """Record a provider health check."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO provider_health_events
                (source, checked_at_utc, status, detail, terminal_version)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    health.source,
                    _utc_isoformat(health.checked_at_utc),
                    health.status.value,
                    health.detail,
                    health.terminal_version,
                ),
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


def as_utc(value: datetime) -> datetime:
    """Convert aware timestamp to UTC or reject naive timestamps."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)
