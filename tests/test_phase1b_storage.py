"""SQLite schema v3, strict deserialization and cache-lookup tests (temporary databases)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kankalar_gold_ai.core.contracts import Direction, StudyResult, StudyStatus
from kankalar_gold_ai.core.study_results import CACHE_FORMAT_VERSION, StudyResultKey
from kankalar_gold_ai.storage.sqlite import (
    MigrationError,
    SQLitePersistence,
    StoredStudyResultError,
)

AS_OF = datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)
SNAPSHOT = "bars-v1:sha256:" + "a" * 64


def _result(**changes: object) -> StudyResult:
    base = StudyResult(
        study_id="s.one",
        version="1.0.0",
        instrument="TEST_GOLD",
        timeframe="M5",
        as_of_utc=AS_OF,
        status=StudyStatus.AVAILABLE,
        state="up",
        value=1.5,
        normalized_value=0.5,
        direction=Direction.BULLISH,
        uncertainty=0.1,
        inputs={"period": 3},
        evidence=("ok",),
        limitations=(),
        data_snapshot_id=SNAPSHOT,
        provenance=(
            ("cache_format_version", CACHE_FORMAT_VERSION),
            ("input_bar_count", "3"),
            ("input_source", "synthetic"),
            ("input_window_end_utc", "2026-01-01T00:15:00Z"),
            ("input_window_start_utc", "2026-01-01T00:00:00Z"),
            ("study_version", "1.0.0"),
        ),
    )
    return replace(base, **changes)


def _key(**changes: object) -> StudyResultKey:
    values: dict[str, object] = {
        "study_id": "s.one",
        "version": "1.0.0",
        "instrument": "TEST_GOLD",
        "timeframe": "M5",
        "as_of_utc": AS_OF,
        "inputs_json": json.dumps({"period": 3}, sort_keys=True),
        "data_snapshot_id": SNAPSHOT,
    }
    values.update(changes)
    return StudyResultKey(**values)  # type: ignore[arg-type]


def _db(tmp_path: Path) -> SQLitePersistence:
    store = SQLitePersistence(tmp_path / "s.db")
    store.initialize()
    return store


def _sql(tmp_path: Path, statement: str, *params: object) -> None:
    with sqlite3.connect(tmp_path / "s.db") as connection:
        connection.execute(statement, params)


def test_json_extract_is_not_required_and_round_trip(tmp_path: Path) -> None:
    store = _db(tmp_path)
    original = _result()
    store.save_study_result(original)
    assert store.find_cached_study_result(_key()) == original


def test_each_identity_component_changes_lookup(tmp_path: Path) -> None:
    store = _db(tmp_path)
    store.save_study_result(_result())
    for changes in (
        {"study_id": "s.two"},
        {"version": "2.0.0"},
        {"instrument": "OTHER"},
        {"timeframe": "M1"},
        {"as_of_utc": AS_OF + timedelta(minutes=15)},
        {"inputs_json": json.dumps({"period": 4})},
        {"data_snapshot_id": "bars-v1:sha256:" + "b" * 64},
    ):
        assert store.find_cached_study_result(_key(**changes)) is None


def test_newest_row_wins_and_history_is_preserved(tmp_path: Path) -> None:
    store = _db(tmp_path)
    store.save_study_result(_result(value=1.0))
    store.save_study_result(_result(value=2.0))
    found = store.find_cached_study_result(_key())
    assert found is not None and found.value == 2.0
    with sqlite3.connect(tmp_path / "s.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_results").fetchone()[0] == 2


def test_error_and_stale_rows_never_hit_or_shadow(tmp_path: Path) -> None:
    store = _db(tmp_path)
    good = _result()
    store.save_study_result(good)
    for status in (StudyStatus.ERROR, StudyStatus.STALE):
        store.save_study_result(
            replace(
                good,
                status=status,
                state=None,
                value=None,
                normalized_value=None,
                uncertainty=None,
                direction=Direction.NOT_APPLICABLE,
            )
        )
    assert store.find_cached_study_result(_key()) == good


def test_old_format_and_null_rows_are_not_hits(tmp_path: Path) -> None:
    store = _db(tmp_path)
    store.save_study_result(_result())
    _sql(tmp_path, "UPDATE study_results SET cache_format_version = 'old'")
    assert store.find_cached_study_result(_key()) is None
    _sql(tmp_path, "UPDATE study_results SET cache_format_version = NULL")
    assert store.find_cached_study_result(_key()) is None


def test_plain_save_of_unversioned_result_is_not_a_hit(tmp_path: Path) -> None:
    store = _db(tmp_path)
    store.save_study_result(_result(provenance=()))
    assert store.find_cached_study_result(_key()) is None


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("provenance_json", "not json"),
        ("provenance_json", None),
        ("provenance_json", "[]"),
        ("provenance_json", '{"cache_format_version": "study-result-v1"}'),
        (
            "provenance_json",
            '{"cache_format_version": "study-result-v1", "study_version": "9", "x": "1"}',
        ),
        ("status", "BOGUS"),
        ("status", "UNAVAILABLE"),
        ("direction", "SIDEWAYS"),
        ("value", float("inf")),
        ("value", "text"),
        ("inputs_json", '{"period": NaN}'),
        ("inputs_json", "[1]"),
        ("evidence_json", '{"a": 1}'),
        ("evidence_json", "[1]"),
        ("limitations_json", "null"),
        ("data_snapshot_id", ""),
    ],
)
def test_corrupt_row_raises_and_is_never_a_hit(tmp_path: Path, column: str, value: object) -> None:
    store = _db(tmp_path)
    store.save_study_result(_result())
    _sql(tmp_path, f"UPDATE study_results SET {column} = ?", value)
    lookup_key = _key(inputs_json=str(value)) if column == "inputs_json" else _key()
    if column == "status" and value == "BOGUS":
        # excluded by the SQL status filter, hence simply not a hit
        assert store.find_cached_study_result(lookup_key) is None
        return
    if column == "data_snapshot_id":
        assert store.find_cached_study_result(lookup_key) is None
        return
    with pytest.raises(StoredStudyResultError):
        store.find_cached_study_result(lookup_key)


def test_corrupt_newest_row_is_not_skipped_for_older_valid_row(tmp_path: Path) -> None:
    store = _db(tmp_path)
    store.save_study_result(_result())
    store.save_study_result(_result(value=2.0))
    _sql(tmp_path, "UPDATE study_results SET direction = 'BAD' WHERE id = 2")
    with pytest.raises(StoredStudyResultError):
        store.find_cached_study_result(_key())


def test_status_matrix_enforced_on_load(tmp_path: Path) -> None:
    store = _db(tmp_path)
    store.save_study_result(
        _result(
            status=StudyStatus.INSUFFICIENT_DATA,
            state=None,
            value=None,
            normalized_value=None,
            uncertainty=None,
            direction=Direction.NOT_APPLICABLE,
        )
    )
    assert store.find_cached_study_result(_key()) is not None
    _sql(tmp_path, "UPDATE study_results SET value = 1.0")
    with pytest.raises(StoredStudyResultError):
        store.find_cached_study_result(_key())


def test_column_and_provenance_cache_version_must_agree(tmp_path: Path) -> None:
    store = _db(tmp_path)
    store.save_study_result(_result())
    _sql(
        tmp_path,
        "UPDATE study_results SET provenance_json = ?",
        json.dumps({**dict(_result().provenance), "cache_format_version": "other"}),
    )
    with pytest.raises(StoredStudyResultError):
        store.find_cached_study_result(_key())


# -- migration -------------------------------------------------------------
V2_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (2);
CREATE TABLE study_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, study_id TEXT NOT NULL, version TEXT NOT NULL,
    instrument TEXT NOT NULL, timeframe TEXT NOT NULL, as_of_utc TEXT NOT NULL,
    status TEXT NOT NULL, state TEXT, value REAL, normalized_value REAL,
    direction TEXT NOT NULL, uncertainty REAL, inputs_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL, limitations_json TEXT NOT NULL, data_snapshot_id TEXT
);
INSERT INTO study_results (study_id, version, instrument, timeframe, as_of_utc, status,
    state, value, normalized_value, direction, uncertainty, inputs_json, evidence_json,
    limitations_json, data_snapshot_id)
VALUES ('s.one', '1.0.0', 'TEST_GOLD', 'M5', '2026-01-01T00:15:00Z', 'AVAILABLE', 'up',
    1.5, 0.5, 'BULLISH', 0.1, '{"period": 3}', '["ok"]', '[]', 'bars-v1:sha256:aaaa');
"""


def _v2_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(V2_SCHEMA)


def _snapshot(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT id, study_id, as_of_utc, status, inputs_json, data_snapshot_id "
            "FROM study_results"
        ).fetchall()


def test_v2_to_v3_migration_preserves_data_and_old_rows_are_not_hits(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    _v2_db(path)
    before = _snapshot(path)
    store = SQLitePersistence(path)
    store.initialize()
    assert SQLitePersistence.SCHEMA_VERSION == 3
    assert _snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        row = connection.execute(
            "SELECT provenance_json, cache_format_version FROM study_results"
        ).fetchone()
        assert row == (None, None)
        indexes = {r[1] for r in connection.execute("PRAGMA index_list(study_results)")}
        assert "idx_study_results_cache" in indexes
    old_key = _key(data_snapshot_id="bars-v1:sha256:aaaa", inputs_json='{"period": 3}')
    assert store.find_cached_study_result(old_key) is None


def test_injected_v3_failure_rolls_back_to_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "s.db"
    _v2_db(path)
    before = _snapshot(path)
    original = SQLitePersistence._migrate_to_version_3

    def fail(persistence: SQLitePersistence, connection: sqlite3.Connection) -> None:
        original(persistence, connection)
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(SQLitePersistence, "_migrate_to_version_3", fail)
    with pytest.raises(MigrationError, match="rolled back"):
        SQLitePersistence(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        columns = [r[1] for r in connection.execute("PRAGMA table_info(study_results)")]
        assert "provenance_json" not in columns and "cache_format_version" not in columns
        names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        assert "idx_study_results_cache" not in names
    assert _snapshot(path) == before
    monkeypatch.undo()
    SQLitePersistence(path).initialize()
    assert _snapshot(path) == before
