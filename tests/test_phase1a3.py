"""Phase 1A.3 integration tests: provider manager, data pipeline, persistence, and quality."""

import sqlite3
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
from kankalar_gold_ai.core.contracts import (
    DataQualityReport,
    DataStatus,
    InstrumentSpecification,
    MarketBar,
    MarketDataHealth,
    MarketQuote,
    MarketTick,
)
from kankalar_gold_ai.data.pipeline import (
    HistoricalDataPipeline,
    HistoricalDataQualityError,
)
from kankalar_gold_ai.data.provider_manager import ProviderConfig, ProviderManager
from kankalar_gold_ai.storage.sqlite import MigrationError, SQLitePersistence
from tests.fixtures.market_data import SYNTHETIC_SOURCE

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

# Create bars aligned with NOW for testing
ALIGNED_BARS = tuple(
    MarketBar(
        instrument="TEST_GOLD",
        timeframe="M5",
        timestamp_utc=NOW - timedelta(minutes=5 * (3 - index)),
        open=100.0 + index,
        high=101.0 + index,
        low=99.0 + index,
        close=100.5 + index,
        volume=None,
        source=SYNTHETIC_SOURCE,
    )
    for index in range(3)
)

LEGACY_PHASE0_SCHEMA = """
    CREATE TABLE market_bars (
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
    CREATE TABLE study_results (
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
    CREATE TABLE signal_events (
        event_id TEXT PRIMARY KEY,
        signal_id TEXT NOT NULL,
        state TEXT NOT NULL,
        occurred_at_utc TEXT NOT NULL,
        reason_code TEXT NOT NULL
    );
"""
LEGACY_PHASE0_TABLES = {"market_bars", "study_results", "signal_events"}


def _create_legacy_phase0_database(db_path: Path) -> None:
    """Create the exact unversioned schema from the original Phase 0 implementation."""
    bar = ALIGNED_BARS[0]
    with sqlite3.connect(str(db_path)) as connection:
        connection.executescript(LEGACY_PHASE0_SCHEMA)
        connection.execute(
            "INSERT INTO market_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bar.instrument,
                bar.timeframe,
                bar.timestamp_utc.isoformat().replace("+00:00", "Z"),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.source,
            ),
        )
        connection.execute(
            """
            INSERT INTO study_results VALUES (
                7, 'legacy.study', '0.1.0', 'TEST_GOLD', 'M5',
                '2026-01-01T12:00:00Z', 'AVAILABLE', 'legacy-state',
                1.25, NULL, 'NEUTRAL', 0.1, '{"window": 2}',
                '["legacy evidence"]', '["legacy limitation"]', 'legacy-snapshot'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO signal_events VALUES (
                'legacy-event', 'legacy-signal', 'CREATED',
                '2026-01-01T12:00:00Z', 'legacy_reason'
            )
            """
        )


def _legacy_phase0_records(db_path: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(str(db_path)) as connection:
        records: dict[str, list[tuple[object, ...]]] = {}
        for table in sorted(LEGACY_PHASE0_TABLES):
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
            if table == "study_results" and rows and len(rows[0]) > 16:
                # Schema v3 appends provenance_json/cache_format_version; legacy rows keep NULL.
                assert all(row[16:] == (None, None) for row in rows)
                rows = [row[:16] for row in rows]
            records[table] = rows
        return records


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }


def _assert_injected_migration_failure_rolls_back(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migration_method: str,
) -> None:
    _create_legacy_phase0_database(db_path)
    original_records = _legacy_phase0_records(db_path)
    original_migration = getattr(SQLitePersistence, migration_method)

    def fail_after_schema_work(
        persistence: SQLitePersistence, connection: sqlite3.Connection
    ) -> None:
        original_migration(persistence, connection)
        raise sqlite3.OperationalError("injected migration failure")

    monkeypatch.setattr(SQLitePersistence, migration_method, fail_after_schema_work)
    persistence = SQLitePersistence(db_path)
    with pytest.raises(MigrationError, match="all migration changes rolled back"):
        persistence.initialize()

    assert _table_names(db_path) == LEGACY_PHASE0_TABLES
    assert _legacy_phase0_records(db_path) == original_records
    with sqlite3.connect(str(db_path)) as connection:
        indexes = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    assert indexes == []

    monkeypatch.undo()
    persistence.initialize()
    assert _legacy_phase0_records(db_path) == original_records
    assert {
        "schema_version",
        "instrument_specifications",
        "market_ticks",
        "market_quotes",
        "data_quality_events",
        "provider_health_events",
    } <= _table_names(db_path)
    with sqlite3.connect(str(db_path)) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [
            (SQLitePersistence.SCHEMA_VERSION,)
        ]

    persistence.initialize()
    assert _legacy_phase0_records(db_path) == original_records
    with sqlite3.connect(str(db_path)) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [
            (SQLitePersistence.SCHEMA_VERSION,)
        ]


class TestProviderManager:
    """Test provider selection, validation, and health reporting."""

    def test_mock_provider_default_empty_bars(self) -> None:
        config = ProviderConfig(source="mock")
        manager = ProviderManager(config)

        assert manager.source() == "mock"
        assert isinstance(manager.provider(), MockMarketDataProvider)
        health = manager.health()
        assert health.status == DataStatus.VALID
        assert health.source == "mock"

    def test_mock_provider_with_bars(self) -> None:
        config = ProviderConfig(source="mock")
        manager = ProviderManager(config, mock_bars=ALIGNED_BARS)

        provider = manager.provider()
        assert isinstance(provider, MockMarketDataProvider)
        bars = provider.get_bars("TEST_GOLD", "M5", NOW - timedelta(hours=1), NOW)
        assert len(bars) == len(ALIGNED_BARS)

    def test_mt5_provider_requires_symbol(self) -> None:
        with pytest.raises(ValueError, match="mt5_symbol"):
            ProviderConfig(source="mt5")

    def test_mt5_provider_symbol_not_blank(self) -> None:
        with pytest.raises(ValueError, match="blank"):
            ProviderConfig(source="mt5", mt5_symbol="")

    def test_mock_provider_rejects_mt5_symbol(self) -> None:
        with pytest.raises(ValueError, match="does not accept"):
            ProviderConfig(source="mock", mt5_symbol="XAUUSD")

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            ProviderConfig(source="unknown_source")

    def test_mt5_provider_unavailable_on_non_windows(self) -> None:
        """MT5 loading is deferred until provider() is called; health() reports unavailability."""
        config = ProviderConfig(source="mt5", mt5_symbol="XAUUSD")
        manager = ProviderManager(config)

        # health() should report unavailability on non-Windows
        health = manager.health()
        assert health.status == DataStatus.UNAVAILABLE
        assert "Windows" in health.detail or "platform" in health.detail.lower()

    def test_unavailable_mt5_provider_cannot_be_retrieved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unavailable_client() -> object:
            raise RuntimeError("offline MT5 client")

        monkeypatch.setattr(
            "kankalar_gold_ai.adapters.mt5.client.load_windows_client",
            unavailable_client,
        )
        manager = ProviderManager(ProviderConfig(source="mt5", mt5_symbol="XAUUSD"))

        assert manager.health().status is DataStatus.UNAVAILABLE
        with pytest.raises(RuntimeError, match="offline MT5 client"):
            manager.provider()

    def test_provider_manager_repr(self) -> None:
        config = ProviderConfig(source="mock")
        manager = ProviderManager(config)
        assert "mock" in repr(manager)


class TestHistoricalDataPipeline:
    """Test historical data retrieval, normalization, and validation."""

    def test_pipeline_retrieves_bars_in_range(self) -> None:
        provider = MockMarketDataProvider(ALIGNED_BARS)
        pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)

        bars = pipeline.retrieve_bars(
            "TEST_GOLD",
            "M5",
            end_utc=NOW,
            history_days=1,
        )

        assert len(bars) == len(ALIGNED_BARS)
        assert all(bar.instrument == "TEST_GOLD" for bar in bars)
        assert all(bar.timeframe == "M5" for bar in bars)

    def test_pipeline_unsupported_timeframe_rejected(self) -> None:
        provider = MockMarketDataProvider(ALIGNED_BARS)
        pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)

        with pytest.raises(ValueError, match="unsupported timeframe"):
            pipeline.retrieve_bars("TEST_GOLD", "INVALID", end_utc=NOW, history_days=1)

    def test_pipeline_history_days_bounds(self) -> None:
        provider = MockMarketDataProvider(ALIGNED_BARS)
        pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)

        with pytest.raises(ValueError, match="history_days"):
            pipeline.retrieve_bars("TEST_GOLD", "M5", end_utc=NOW, history_days=0)

        with pytest.raises(ValueError, match="history_days"):
            pipeline.retrieve_bars("TEST_GOLD", "M5", end_utc=NOW, history_days=400)

    def test_pipeline_rejects_future_end(self) -> None:
        provider = MockMarketDataProvider(ALIGNED_BARS)
        pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)

        with pytest.raises(ValueError, match="must not be in the future"):
            pipeline.retrieve_bars(
                "TEST_GOLD", "M5", end_utc=NOW + timedelta(days=1), history_days=1
            )

    def test_pipeline_latest_complete_bar_none_when_no_bars(self) -> None:
        provider = MockMarketDataProvider(())
        pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)

        result = pipeline.latest_complete_bar("TEST_GOLD", "M5")
        assert result is None

    def test_pipeline_validates_bars(self) -> None:
        provider = MockMarketDataProvider(ALIGNED_BARS)
        pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)

        report = pipeline.validate_bars(ALIGNED_BARS, "M5")
        assert report.status == DataStatus.VALID

    @pytest.mark.parametrize(
        "status",
        (
            DataStatus.STALE,
            DataStatus.ERROR,
            DataStatus.INSUFFICIENT_DATA,
            DataStatus.UNAVAILABLE,
        ),
    )
    def test_pipeline_rejects_every_non_valid_quality_status(
        self,
        status: DataStatus,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report = DataQualityReport(status, NOW, (f"{status.value}_reason",))
        monkeypatch.setattr(
            "kankalar_gold_ai.data.pipeline.validate_bars",
            lambda *args, **kwargs: report,
        )
        pipeline = HistoricalDataPipeline(
            MockMarketDataProvider(ALIGNED_BARS),
            clock=lambda: NOW + timedelta(seconds=30),
        )

        with pytest.raises(HistoricalDataQualityError) as error:
            pipeline.retrieve_bars("TEST_GOLD", "M5", NOW, history_days=1)

        assert error.value.report is report
        assert error.value.report.status is status
        assert error.value.report.reasons == (f"{status.value}_reason",)

    def test_pipeline_rejects_empty_history_with_diagnostics(self) -> None:
        pipeline = HistoricalDataPipeline(MockMarketDataProvider(()), clock=lambda: NOW)

        with pytest.raises(HistoricalDataQualityError) as error:
            pipeline.retrieve_bars("TEST_GOLD", "M5", NOW, history_days=1)

        assert error.value.report.status is DataStatus.INSUFFICIENT_DATA
        assert error.value.report.reasons == ("EMPTY_HISTORY",)

    def test_latest_complete_bar_rejects_non_valid_quality_status(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report = DataQualityReport(DataStatus.ERROR, NOW, ("INVALID_BAR",))
        monkeypatch.setattr(
            "kankalar_gold_ai.data.pipeline.validate_bars",
            lambda *args, **kwargs: report,
        )
        pipeline = HistoricalDataPipeline(
            MockMarketDataProvider(ALIGNED_BARS),
            clock=lambda: NOW + timedelta(seconds=30),
        )

        with pytest.raises(HistoricalDataQualityError) as error:
            pipeline.latest_complete_bar("TEST_GOLD", "M5")

        assert error.value.report is report

    def test_pipeline_check_coverage_valid(self) -> None:
        provider = MockMarketDataProvider(ALIGNED_BARS)
        pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)

        start = ALIGNED_BARS[0].timestamp_utc
        end = ALIGNED_BARS[-1].timestamp_utc
        report = pipeline.check_coverage("TEST_GOLD", "M5", start, end)

        assert report.status == DataStatus.VALID

    def test_pipeline_check_coverage_invalid_range(self) -> None:
        provider = MockMarketDataProvider(ALIGNED_BARS)
        pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)

        with pytest.raises(ValueError, match="start_utc must not be after end_utc"):
            pipeline.check_coverage("TEST_GOLD", "M5", NOW, NOW - timedelta(hours=1))

    def test_pipeline_repr(self) -> None:
        provider = MockMarketDataProvider(())
        pipeline = HistoricalDataPipeline(provider)
        assert "HistoricalDataPipeline" in repr(pipeline)


class TestSQLitePersistence:
    """Test database migrations, persistence, and data integrity."""

    @pytest.fixture
    def db_path(self) -> Path:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "test.db"

    def test_initialize_creates_schema(self, db_path: Path) -> None:
        persistence = SQLitePersistence(db_path)
        persistence.initialize()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}

        assert "market_bars" in tables
        assert "study_results" in tables
        assert "signal_events" in tables
        assert "instrument_specifications" in tables
        assert "market_ticks" in tables
        assert "market_quotes" in tables
        assert "data_quality_events" in tables
        assert "provider_health_events" in tables

        conn.close()

    def test_save_market_bars(self, db_path: Path) -> None:
        persistence = SQLitePersistence(db_path)
        persistence.initialize()
        persistence.save_market_bars(ALIGNED_BARS)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM market_bars")
        count = cursor.fetchone()[0]
        assert count == len(ALIGNED_BARS)
        conn.close()

    def test_save_market_bars_duplicate_rejected(self, db_path: Path) -> None:
        persistence = SQLitePersistence(db_path)
        persistence.initialize()
        persistence.save_market_bars(ALIGNED_BARS[:1])

        with pytest.raises(sqlite3.IntegrityError):
            persistence.save_market_bars(ALIGNED_BARS[:1])

    def test_persistence_source_isolation_for_bars_ticks_and_quotes(self, db_path: Path) -> None:
        persistence = SQLitePersistence(db_path)
        persistence.initialize()

        mock_bar = replace(ALIGNED_BARS[0], source="mock")
        mt5_bar = replace(ALIGNED_BARS[0], source="mt5")
        persistence.save_market_bars((mock_bar,))
        persistence.save_market_bars((mt5_bar,))

        mock_tick = MarketTick(
            instrument="TEST_GOLD",
            timestamp_utc=NOW,
            timestamp_milliseconds=int(NOW.timestamp() * 1000),
            bid=100.0,
            ask=100.1,
            last=100.05,
            volume=1.5,
            flags=0,
            source="mock",
            retrieved_at_utc=NOW,
        )
        mt5_tick = replace(mock_tick, source="mt5")
        persistence.save_market_ticks((mock_tick,))
        persistence.save_market_ticks((mt5_tick,))

        mock_quote = MarketQuote(
            instrument="TEST_GOLD",
            timestamp_utc=NOW,
            timestamp_milliseconds=int(NOW.timestamp() * 1000),
            bid=100.0,
            ask=100.1,
            last=None,
            source="mock",
            retrieved_at_utc=NOW,
        )
        mt5_quote = replace(mock_quote, source="mt5")
        persistence.save_market_quote(mock_quote)
        persistence.save_market_quote(mt5_quote)

        with sqlite3.connect(str(db_path)) as connection:
            bar_sources = connection.execute(
                "SELECT source FROM market_bars ORDER BY source"
            ).fetchall()
            tick_sources = connection.execute(
                "SELECT source FROM market_ticks ORDER BY source"
            ).fetchall()
            quote_sources = connection.execute(
                "SELECT source FROM market_quotes ORDER BY source"
            ).fetchall()

        assert bar_sources == [("mock",), ("mt5",)]
        assert tick_sources == [("mock",), ("mt5",)]
        assert quote_sources == [("mock",), ("mt5",)]

        with pytest.raises(sqlite3.IntegrityError):
            persistence.save_market_bars((mock_bar,))
        with pytest.raises(sqlite3.IntegrityError):
            persistence.save_market_ticks((mock_tick,))
        with pytest.raises(sqlite3.IntegrityError):
            persistence.save_market_quote(mock_quote)

    def test_save_market_bars_batch_failure_rolls_back_partial_rows(self, db_path: Path) -> None:
        persistence = SQLitePersistence(db_path)
        persistence.initialize()
        baseline = ALIGNED_BARS[0]
        unrelated = ALIGNED_BARS[1]
        partial = replace(
            baseline,
            timestamp_utc=baseline.timestamp_utc - timedelta(days=1),
        )
        persistence.save_market_bars((baseline, unrelated))

        with pytest.raises(sqlite3.IntegrityError):
            persistence.save_market_bars((partial, baseline))

        with sqlite3.connect(str(db_path)) as connection:
            records = connection.execute(
                "SELECT timestamp_utc, source FROM market_bars ORDER BY timestamp_utc"
            ).fetchall()

        assert records == sorted(
            [
                (baseline.timestamp_utc.isoformat().replace("+00:00", "Z"), baseline.source),
                (unrelated.timestamp_utc.isoformat().replace("+00:00", "Z"), unrelated.source),
            ]
        )
        assert all(
            row[0] != partial.timestamp_utc.isoformat().replace("+00:00", "Z") for row in records
        )

    def test_save_instrument_specification(self, db_path: Path) -> None:
        persistence = SQLitePersistence(db_path)
        persistence.initialize()

        spec = InstrumentSpecification(
            symbol="XAUUSD",
            source="mt5",
            retrieved_at_utc=NOW,
            digits=2,
            point=0.01,
            contract_size=100.0,
            tick_size=0.01,
            tick_value=1.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
        )

        persistence.save_instrument_specification(spec)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, digits, point FROM instrument_specifications LIMIT 1")
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "XAUUSD"
        assert row[1] == 2
        assert row[2] == 0.01
        conn.close()

    def test_save_market_ticks(self, db_path: Path) -> None:
        persistence = SQLitePersistence(db_path)
        persistence.initialize()

        ticks = (
            MarketTick(
                instrument="TEST_GOLD",
                timestamp_utc=NOW,
                timestamp_milliseconds=int(NOW.timestamp() * 1000),
                bid=100.0,
                ask=100.1,
                last=100.05,
                volume=1.5,
                flags=0,
                source=SYNTHETIC_SOURCE,
                retrieved_at_utc=NOW,
            ),
        )

        persistence.save_market_ticks(ticks)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM market_ticks")
        count = cursor.fetchone()[0]
        assert count == 1
        conn.close()

    def test_save_market_quote(self, db_path: Path) -> None:
        persistence = SQLitePersistence(db_path)
        persistence.initialize()

        quote = MarketQuote(
            instrument="TEST_GOLD",
            timestamp_utc=NOW,
            timestamp_milliseconds=int(NOW.timestamp() * 1000),
            bid=100.0,
            ask=100.1,
            last=None,
            source=SYNTHETIC_SOURCE,
            retrieved_at_utc=NOW,
        )

        persistence.save_market_quote(quote)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM market_quotes")
        count = cursor.fetchone()[0]
        assert count == 1
        conn.close()

    def test_save_data_quality_event(self, db_path: Path) -> None:
        persistence = SQLitePersistence(db_path)
        persistence.initialize()

        persistence.save_data_quality_event(
            instrument="TEST_GOLD",
            timeframe="M5",
            timestamp_utc=NOW,
            status="VALID",
            reasons=("passed",),
            source=SYNTHETIC_SOURCE,
        )

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM data_quality_events")
        count = cursor.fetchone()[0]
        assert count == 1
        conn.close()

    def test_save_provider_health_event(self, db_path: Path) -> None:
        persistence = SQLitePersistence(db_path)
        persistence.initialize()

        health = MarketDataHealth(
            status=DataStatus.VALID,
            source="mock",
            checked_at_utc=NOW,
            detail="All systems nominal",
            terminal_version=None,
        )

        persistence.save_provider_health_event(health)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM provider_health_events")
        count = cursor.fetchone()[0]
        assert count == 1
        conn.close()

    def test_migration_idempotent(self, db_path: Path) -> None:
        persistence = SQLitePersistence(db_path)
        persistence.initialize()
        persistence.initialize()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        version = cursor.fetchone()[0]
        assert version == SQLitePersistence.SCHEMA_VERSION
        conn.close()

    def test_initialize_rejects_newer_schema_without_downgrading(self, db_path: Path) -> None:
        persistence = SQLitePersistence(db_path)
        persistence.initialize()

        with sqlite3.connect(str(db_path)) as connection:
            connection.execute(
                "UPDATE schema_version SET version = ?",
                (SQLitePersistence.SCHEMA_VERSION + 1,),
            )

        with pytest.raises(MigrationError, match="newer than supported"):
            persistence.initialize()

        with sqlite3.connect(str(db_path)) as connection:
            assert connection.execute("SELECT version FROM schema_version").fetchall() == [
                (SQLitePersistence.SCHEMA_VERSION + 1,)
            ]

    def test_phase_0_data_preserved_after_migration(self, db_path: Path) -> None:
        persistence = SQLitePersistence(db_path)
        persistence.initialize()
        persistence.save_market_bars(ALIGNED_BARS)

        persistence2 = SQLitePersistence(db_path)
        persistence2.initialize()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM market_bars")
        count = cursor.fetchone()[0]
        assert count == len(ALIGNED_BARS)
        conn.close()

    def test_genuine_phase0_database_migration_preserves_every_legacy_record(
        self, db_path: Path
    ) -> None:
        _create_legacy_phase0_database(db_path)
        original_records = _legacy_phase0_records(db_path)

        persistence = SQLitePersistence(db_path)
        persistence.initialize()

        with sqlite3.connect(str(db_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            version = connection.execute("SELECT version FROM schema_version").fetchone()[0]

        assert original_records == _legacy_phase0_records(db_path)
        assert version == SQLitePersistence.SCHEMA_VERSION
        assert {
            "instrument_specifications",
            "market_ticks",
            "market_quotes",
            "data_quality_events",
            "provider_health_events",
        } <= tables

        persistence.initialize()
        assert original_records == _legacy_phase0_records(db_path)
        with sqlite3.connect(str(db_path)) as connection:
            assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == (
                SQLitePersistence.SCHEMA_VERSION
            )

    def test_phase0_migration_failure_rolls_back_and_retries(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _assert_injected_migration_failure_rolls_back(db_path, monkeypatch, "_migrate_to_version_1")

    def test_phase1a_migration_failure_rolls_back_entire_upgrade_and_retries(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _assert_injected_migration_failure_rolls_back(db_path, monkeypatch, "_migrate_to_version_2")
