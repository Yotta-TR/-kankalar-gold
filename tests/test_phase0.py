import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pytest
from pydantic import ValidationError

from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
from kankalar_gold_ai.core.config import ProjectConfig, load_config
from kankalar_gold_ai.core.contracts import (
    Direction,
    SignalLifecycleEvent,
    SignalState,
    StudyResult,
    StudyStatus,
    as_utc,
)
from kankalar_gold_ai.observability.logging import configure_logging, log_exception
from kankalar_gold_ai.security.trading_safety import (
    READ_ONLY_MT5_APIS,
    analyze_source,
    scan_package,
)
from kankalar_gold_ai.storage.sqlite import SQLitePersistence
from tests.fixtures.market_data import SYNTHETIC_BARS, SYNTHETIC_SOURCE
from tests.fixtures.trading_safety import (
    ALIASED_HTTP_ROUTE,
    ALIASED_READ_ONLY_API,
    MT5_EXECUTION_ALIASES,
    ORDINARY_MAPPING_LOOKUP,
    RENAMED_HTTP_ROUTE,
    RENAMED_TELEGRAM_HANDLER,
)

ROOT = Path(__file__).parents[1]


def test_default_config_is_research_only() -> None:
    config = load_config(ROOT / "config" / "defaults.yaml")

    assert config.app.safe_mode is True
    assert config.signals.allow_live_trading is False
    assert config.notifications.telegram_enabled is False
    assert config.risk.mode == "virtual"
    assert config.instrument.source == "mock"


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("app", "safe_mode", False),
        ("signals", "allow_live_trading", True),
        ("notifications", "telegram_enabled", True),
        ("risk", "mode", "live"),
    ],
)
def test_config_rejects_non_research_settings(
    section: str,
    key: str,
    value: object,
) -> None:
    config_data = ProjectConfig().model_dump()
    config_data[section][key] = value

    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(config_data)


def test_as_utc_converts_offsets_and_rejects_naive_values() -> None:
    local_timestamp = datetime(2026, 1, 1, 2, tzinfo=timezone(timedelta(hours=2)))

    assert as_utc(local_timestamp) == datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="timezone-aware"):
        as_utc(datetime(2026, 1, 1))


def test_study_status_enum_matches_common_contract() -> None:
    assert {status.value for status in StudyStatus} == {
        "AVAILABLE",
        "INSUFFICIENT_DATA",
        "UNAVAILABLE",
        "STALE",
        "ERROR",
    }


def test_study_result_has_common_schema_and_utc_timestamp() -> None:
    result = StudyResult(
        study_id="test.synthetic",
        version="0.1.0",
        instrument="TEST_GOLD",
        timeframe="M5",
        as_of_utc=datetime(2026, 1, 1, 2, tzinfo=timezone(timedelta(hours=2))),
        status=StudyStatus.AVAILABLE,
        state="SYNTHETIC",
        value=1.0,
        normalized_value=None,
        direction=Direction.NEUTRAL,
        uncertainty=None,
        inputs={"window": 3},
        evidence=("synthetic fixture only",),
        limitations=("not real market data",),
        data_snapshot_id="fixture-1",
    )

    assert result.as_of_utc == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert result.status is StudyStatus.AVAILABLE
    assert result.direction is Direction.NEUTRAL
    assert result.data_snapshot_id == "fixture-1"


def test_mock_provider_filters_supplied_synthetic_bars() -> None:
    provider = MockMarketDataProvider(SYNTHETIC_BARS)
    found = provider.get_bars(
        "TEST_GOLD",
        "M5",
        datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc),
    )

    assert len(found) == 2
    assert all(bar.source == SYNTHETIC_SOURCE for bar in found)


def test_logging_redacts_credentials_in_messages_and_exception_traces() -> None:
    output = StringIO()
    handler = logging.StreamHandler(output)
    logger = configure_logging(handler=handler)
    logger.info("api_key=%s", "key-value")
    try:
        raise ValueError("password=secret-value")
    except ValueError as error:
        log_exception(logger, "provider failed", error)

    rendered = output.getvalue()
    assert "key-value" not in rendered
    assert "secret-value" not in rendered
    assert rendered.count("[REDACTED]") >= 2
    assert "Z INFO " in rendered


def test_logging_preserves_ordinary_message_text() -> None:
    output = StringIO()
    handler = logging.StreamHandler(output)
    logger = configure_logging(handler=handler)
    logger.info("ordinary message remains unchanged")

    assert output.getvalue().split(" INFO kankalar_gold_ai ", 1)[1] == (
        "ordinary message remains unchanged\n"
    )


def test_logging_recursively_redacts_structured_json_and_exception_secrets() -> None:
    output = StringIO()
    handler = logging.StreamHandler(output)
    logger = configure_logging(handler=handler)
    handler.setFormatter(
        logging.Formatter(
            "%(message)s %(context)s %(bot_token)s %(authorization)s",
            defaults={"context": "", "bot_token": "", "authorization": ""},
        )
    )
    logger.info(
        "payload=%s",
        json.dumps(
            {
                "api_key": "api-secret",
                "items": [
                    {"access_token": "access-secret"},
                    {"refresh_token": "refresh-secret"},
                    {"authorization_header": "header-secret"},
                    {"ordinary": "keep-this"},
                ],
            }
        ),
    )
    logger.info(
        "structured",
        extra={
            "context": {
                "nested": {"credentials": "credential-secret", "password": "pw-secret"},
                "items": [{"client_secret": "client-secret"}],
            },
            "bot_token": "bot-secret",
            "authorization": "Bearer structured-auth-secret",
        },
    )
    logger.info("plain Bearer free-text-bearer-secret and ordinary text")
    try:
        raise ValueError('{"secret":"exception-secret"}')
    except ValueError as error:
        log_exception(logger, "request failed", error)

    rendered = output.getvalue()
    for secret in (
        "api-secret",
        "access-secret",
        "refresh-secret",
        "header-secret",
        "credential-secret",
        "pw-secret",
        "client-secret",
        "bot-secret",
        "structured-auth-secret",
        "free-text-bearer-secret",
        "exception-secret",
    ):
        assert secret not in rendered
    assert "keep-this" in rendered
    assert "ordinary text" in rendered
    assert "[REDACTED]" in rendered


def test_sqlite_persistence_stores_phase0_records_with_utc_timestamps(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "research.sqlite"
    store = SQLitePersistence(database_path)
    store.initialize()
    store.save_market_bars(SYNTHETIC_BARS[:1])
    store.save_study_result(
        StudyResult(
            study_id="test.synthetic",
            version="0.1.0",
            instrument="TEST_GOLD",
            timeframe="M5",
            as_of_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            status=StudyStatus.AVAILABLE,
            state="SYNTHETIC",
            value=1.0,
            normalized_value=None,
            direction=Direction.NEUTRAL,
            uncertainty=None,
            inputs={"window": 3},
            evidence=("synthetic fixture only",),
            limitations=("not real market data",),
            data_snapshot_id="fixture-1",
        )
    )
    store.save_signal_event(
        SignalLifecycleEvent(
            event_id="event-1",
            signal_id="signal-1",
            state=SignalState.CREATED,
            occurred_at_utc=datetime(2026, 1, 1, 2, tzinfo=timezone(timedelta(hours=2))),
            reason_code="synthetic_test",
        )
    )

    with sqlite3.connect(database_path) as connection:
        bar_timestamp = connection.execute("SELECT timestamp_utc FROM market_bars").fetchone()[0]
        event_timestamp = connection.execute(
            "SELECT occurred_at_utc FROM signal_events"
        ).fetchone()[0]
        study_row = connection.execute(
            "SELECT as_of_utc, evidence_json FROM study_results"
        ).fetchone()

    assert bar_timestamp == "2026-01-01T00:00:00Z"
    assert event_timestamp == "2026-01-01T00:00:00Z"
    assert study_row == ("2026-01-01T00:00:00Z", '["synthetic fixture only"]')


def test_safety_checker_accepts_only_allowlisted_read_only_mt5_apis() -> None:
    assert "symbol_info" in READ_ONLY_MT5_APIS
    assert "copy_rates_range" in READ_ONLY_MT5_APIS
    assert "order_send" not in READ_ONLY_MT5_APIS
    for source in ALIASED_READ_ONLY_API:
        assert analyze_source(source, "adapters/mt5/provider.py") == ()


def test_safety_checker_allows_ordinary_mapping_lookup() -> None:
    assert analyze_source(ORDINARY_MAPPING_LOOKUP, "core/configuration.py") == ()


@pytest.mark.parametrize("source", MT5_EXECUTION_ALIASES)
def test_safety_checker_rejects_direct_aliased_and_dynamic_mt5_execution(
    source: str,
) -> None:
    violations = analyze_source(source, "adapters/mt5/provider.py")

    assert violations
    assert any(
        violation.rule
        in {
            "mt5-allowlist",
            "dynamic-access",
            "mt5-dynamic-access",
            "dynamic-import",
            "execution-api",
        }
        for violation in violations
    )


def test_safety_checker_rejects_mt5_imports_outside_connector_boundary() -> None:
    violations = analyze_source(
        "from MetaTrader5 import symbol_info",
        "notifications/telegram.py",
    )

    assert any(violation.rule == "mt5-boundary" for violation in violations)


def test_safety_checker_rejects_routes_independent_of_path_or_handler_name() -> None:
    violations = analyze_source(RENAMED_HTTP_ROUTE, "decision/api.py") + analyze_source(
        ALIASED_HTTP_ROUTE, "decision/api.py"
    )

    assert any(violation.rule == "web-route-boundary" for violation in violations)


def test_safety_checker_rejects_telegram_handler_registrations() -> None:
    violations = analyze_source(RENAMED_TELEGRAM_HANDLER, "adapters/telegram/bot.py")

    assert any(violation.rule == "telegram-handler-boundary" for violation in violations)


def test_safety_checker_rejects_unparseable_source() -> None:
    violations = analyze_source("def broken(:", "adapters/mt5/broken.py")

    assert len(violations) == 1
    assert violations[0].rule == "syntax"


def test_application_package_passes_static_trading_safety_scan() -> None:
    package_root = ROOT / "src" / "kankalar_gold_ai"

    assert not (package_root / "execution.py").exists()
    assert not (package_root / "execution").exists()
    assert not (package_root / "trading.py").exists()
    assert scan_package(package_root) == ()


def test_project_document_reference_and_env_exclusion_are_intact() -> None:
    master_spec = ROOT / "KANKALAR_GOLD_AI_MASTER_SPEC_v0.1.md"
    env_example = ROOT / ".env.example"

    assert master_spec.is_file()
    assert env_example.read_text(encoding="utf-8").strip().startswith("#")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in gitignore
    assert ".env.*" in gitignore
    assert "!.env.example" in gitignore

    markdown_link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for markdown_file in (ROOT / "docs").glob("*.md"):
        for target in markdown_link_pattern.findall(markdown_file.read_text(encoding="utf-8")):
            if target.startswith(("https://", "http://", "mailto:")):
                continue
            linked_path = (markdown_file.parent / target.split("#", 1)[0]).resolve()
            assert linked_path.exists(), f"broken documentation link: {markdown_file} -> {target}"


def test_env_example_has_no_non_placeholder_assignments() -> None:
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        value = stripped.split("=", 1)[1].strip()
        assert value in {"", "CHANGE_ME", "your_value_here"}
