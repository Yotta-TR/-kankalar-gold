"""Read-only MT5 tests: all terminal calls are handled by a deterministic fake."""

import sys
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone

import pytest

from kankalar_gold_ai.adapters.mt5.client import load_windows_client
from kankalar_gold_ai.adapters.mt5.provider import (
    MT5EmptyHistoryError,
    MT5Error,
    MT5MarketDataProvider,
)
from kankalar_gold_ai.core.contracts import DataStatus, MarketDataProvider
from kankalar_gold_ai.data.calendar import (
    CalendarClosure,
    CalendarProvenance,
    SessionCalendar,
    WeeklySession,
)
from kankalar_gold_ai.data.errors import HistoricalDataQualityError
from kankalar_gold_ai.data.pipeline import HistoricalDataPipeline
from kankalar_gold_ai.data.quality import validate_bars
from kankalar_gold_ai.security.trading_safety import analyze_source, scan_package

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
BAR_TIME = NOW - timedelta(days=2)
SYMBOL = "XAUUSDm"


class Record:
    def __init__(self, **values: object) -> None:
        self.values = values

    def _asdict(self) -> dict[str, object]:
        return self.values


class FakeMT5:
    TIMEFRAME_M1 = 101
    TIMEFRAME_M5 = 105
    TIMEFRAME_M15 = 115
    TIMEFRAME_H1 = 201
    TIMEFRAME_H4 = 204
    TIMEFRAME_D1 = 301
    COPY_TICKS_ALL = 999

    def __init__(self) -> None:
        self.initialize_result = True
        self.initialize_exception: Exception | None = None
        self.initialize_results: list[bool] | None = None
        self.initialize_timeout: int | None = None
        self.shutdown_calls = 0
        self.initialized = 0
        self.client_initialized = False
        self.shutdown_exception: Exception | None = None
        self.terminal_connected = True
        self.rate_args: tuple[object, ...] | None = None
        self.tick_args: tuple[object, ...] | None = None
        self.rates: list[dict[str, int | float]] | None = [
            {
                "time": int(BAR_TIME.timestamp()),
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "tick_volume": 17,
                "real_volume": 7,
                "spread": 3,
            }
        ]
        self.ticks: list[dict[str, int | float]] | None = [
            {
                "time": int(BAR_TIME.timestamp()),
                "time_msc": int(BAR_TIME.timestamp() * 1000),
                "bid": 100.0,
                "ask": 100.2,
                "last": 0.0,
                "volume_real": 0.0,
                "flags": 2,
            }
        ]
        self.tick_info: Record | None = Record(**self.ticks[0])

    def initialize(self, *, timeout: int = 60_000) -> bool:
        self.initialized += 1
        self.initialize_timeout = timeout
        if self.client_initialized:
            raise RuntimeError("client is already initialized")
        if self.initialize_exception is not None:
            self.client_initialized = True
            raise self.initialize_exception
        result = (
            self.initialize_result
            if self.initialize_results is None
            else self.initialize_results.pop(0)
        )
        if result:
            self.client_initialized = True
            self.terminal_connected = True
        return result

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.shutdown_exception is not None:
            raise self.shutdown_exception
        self.client_initialized = False

    def version(self) -> tuple[int, int, str]:
        return (500, 123, "test")

    def terminal_info(self) -> Record:
        return Record(connected=self.terminal_connected)

    def last_error(self) -> tuple[int, str]:
        return (-123, "synthetic terminal error")

    def symbols_get(self) -> tuple[Record, ...]:
        return (Record(name="EURUSD"), Record(name=SYMBOL), Record(name="GOLD"))

    def symbol_info(self, symbol: str) -> Record | None:
        if symbol not in (SYMBOL, "GOLD"):
            return None
        return Record(
            name=symbol,
            digits=2,
            point=0.01,
            trade_contract_size=100.0,
            trade_tick_size=0.01,
            trade_tick_value=1.0,
            volume_min=0.01,
            volume_max=10.0,
            volume_step=0.01,
            trade_mode=4,
            trade_stops_level=10,
            trade_freeze_level=2,
            currency_profit="USD",
            currency_margin="USD",
        )

    def symbol_info_tick(self, symbol: str) -> Record | None:
        return self.tick_info

    def copy_rates_range(
        self, symbol: str, timeframe: int, start: datetime, end: datetime
    ) -> list[dict[str, int | float]] | None:
        self.rate_args = (symbol, timeframe, start, end)
        return self.rates

    def copy_ticks_range(
        self, symbol: str, start: datetime, end: datetime, flags: int
    ) -> list[dict[str, int | float]] | None:
        self.tick_args = (symbol, start, end, flags)
        return self.ticks


@pytest.fixture
def client() -> FakeMT5:
    return FakeMT5()


@pytest.fixture
def provider(client: FakeMT5) -> MT5MarketDataProvider:
    adapter = MT5MarketDataProvider(client, SYMBOL, clock=lambda: NOW)
    adapter.connect()
    return adapter


def _tick_row(timestamp: datetime = BAR_TIME, **changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "time": int(timestamp.timestamp()),
        "time_msc": int(timestamp.timestamp() * 1000),
        "bid": 100.0,
        "ask": 101.0,
        "last": 0.0,
        "volume": 0,
        "volume_real": 0.0,
        "flags": 2,
    }
    row.update(changes)
    return row


def test_client_load_is_lazy_and_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    from kankalar_gold_ai.adapters.mt5 import client

    assert "MetaTrader5" not in sys.modules
    monkeypatch.setattr(client.platform, "system", lambda: "Linux")
    with pytest.raises(RuntimeError, match="requires Windows"):
        load_windows_client()
    assert "MetaTrader5" not in sys.modules


def test_windows_loader_only_loads_the_injected_module(monkeypatch: pytest.MonkeyPatch) -> None:
    from kankalar_gold_ai.adapters.mt5 import client

    fake = FakeMT5()
    monkeypatch.setattr(client.platform, "system", lambda: "Windows")
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    assert load_windows_client() is fake
    assert fake.initialized == 0


def test_lifecycle_and_health_use_only_injected_client(client: FakeMT5) -> None:
    adapter = MT5MarketDataProvider(client, clock=lambda: NOW)
    assert adapter.health().status == DataStatus.UNAVAILABLE
    assert client.initialized == 0
    adapter.connect()
    adapter.connect()
    assert client.initialized == 1
    assert client.initialize_timeout == 60_000
    assert adapter.health().status == DataStatus.VALID
    assert adapter.health().terminal_version == "500.123.test"
    adapter.shutdown()
    adapter.shutdown()
    assert client.shutdown_calls == 1
    assert not client.client_initialized
    assert adapter.health().status == DataStatus.UNAVAILABLE


def test_initialization_failure_reports_last_error(client: FakeMT5) -> None:
    client.initialize_result = False
    adapter = MT5MarketDataProvider(client, SYMBOL, clock=lambda: NOW)
    with pytest.raises(MT5Error, match=r"initialize failed.*-123"):
        adapter.connect()
    assert adapter.health().status is DataStatus.UNAVAILABLE
    with pytest.raises(MT5Error, match="not initialized"):
        adapter.latest_quote(SYMBOL)


def test_failed_initialization_is_cleaned_up_before_retry(client: FakeMT5) -> None:
    client.initialize_result = False
    adapter = MT5MarketDataProvider(client, SYMBOL, clock=lambda: NOW)

    with pytest.raises(MT5Error, match=r"initialize failed.*-123"):
        adapter.connect()

    assert client.initialized == 3
    assert client.shutdown_calls == 2
    assert adapter.health().status is DataStatus.UNAVAILABLE
    with pytest.raises(MT5Error, match="not initialized"):
        adapter.latest_quote(SYMBOL)

    client.initialize_result = True
    adapter.connect()

    assert client.shutdown_calls == 3
    assert client.initialized == 4
    assert client.client_initialized
    assert adapter.health().status is DataStatus.VALID


def test_connection_retries_are_bounded_and_back_off(
    client: FakeMT5, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kankalar_gold_ai.adapters.mt5 import provider as provider_module

    delays: list[float] = []
    monkeypatch.setattr(provider_module.time, "sleep", delays.append)
    client.initialize_results = [False, True]
    adapter = MT5MarketDataProvider(client, SYMBOL, clock=lambda: NOW)

    adapter.connect()

    assert client.initialized == 2
    assert client.shutdown_calls == 1
    assert client.initialize_timeout == 60_000
    assert delays == [0.25]
    assert adapter.health().status is DataStatus.VALID


def test_initialize_rejects_non_boolean_success_status(
    client: FakeMT5, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kankalar_gold_ai.adapters.mt5 import provider as provider_module

    monkeypatch.setattr(provider_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(client, "initialize", lambda **_kwargs: "connected")
    adapter = MT5MarketDataProvider(client, SYMBOL, clock=lambda: NOW)

    with pytest.raises(MT5Error, match="malformed status"):
        adapter.connect()

    assert adapter.health().status is DataStatus.UNAVAILABLE
    with pytest.raises(MT5Error, match="not initialized"):
        adapter.latest_quote(SYMBOL)


def test_history_timeout_exception_retries_with_a_fixed_bound(
    provider: MT5MarketDataProvider,
    client: FakeMT5,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kankalar_gold_ai.adapters.mt5 import provider as provider_module

    delays: list[float] = []
    monkeypatch.setattr(provider_module.time, "sleep", delays.append)
    calls = 0
    original = client.copy_rates_range

    def timeout_once(
        symbol: str, timeframe: int, start: datetime, end: datetime
    ) -> list[dict[str, int | float]] | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("synthetic request timeout")
        return original(symbol, timeframe, start, end)

    monkeypatch.setattr(client, "copy_rates_range", timeout_once)

    bars = provider.get_bars(SYMBOL, "M5", BAR_TIME, NOW)

    assert len(bars) == 1
    assert calls == 2
    assert delays == [0.25]


def test_history_retry_exhaustion_preserves_timeout_cause(
    provider: MT5MarketDataProvider,
    client: FakeMT5,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kankalar_gold_ai.adapters.mt5 import provider as provider_module

    delays: list[float] = []
    monkeypatch.setattr(provider_module.time, "sleep", delays.append)
    failure = TimeoutError("synthetic request timeout")
    calls = 0

    def always_timeout(
        symbol: str, timeframe: int, start: datetime, end: datetime
    ) -> list[dict[str, int | float]]:
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(client, "copy_rates_range", always_timeout)

    with pytest.raises(MT5Error, match="copy_rates_range failed") as error:
        provider.get_bars(SYMBOL, "M5", BAR_TIME, NOW)

    assert calls == 3
    assert delays == [0.25, 0.5]
    assert error.value.__cause__ is failure


def test_initialization_exception_does_not_establish_connection(
    client: FakeMT5,
) -> None:
    adapter = MT5MarketDataProvider(client, SYMBOL, clock=lambda: NOW)
    failure = RuntimeError("synthetic initialize failure")
    client.initialize_exception = failure

    with pytest.raises(MT5Error, match="initialize failed") as error:
        adapter.connect()

    assert error.value.__cause__ is failure
    assert client.client_initialized
    assert adapter.health().status is DataStatus.UNAVAILABLE
    with pytest.raises(MT5Error, match="not initialized"):
        adapter.latest_quote(SYMBOL)
    adapter.shutdown()
    assert client.shutdown_calls == 3
    assert not client.client_initialized


def test_health_failure_reports_terminal_error(
    provider: MT5MarketDataProvider, client: FakeMT5, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "terminal_info", lambda: None)
    with pytest.raises(MT5Error, match=r"terminal_info failed.*-123"):
        provider.health()
    with pytest.raises(MT5Error, match="not initialized"):
        provider.latest_quote(SYMBOL)


def test_health_exception_invalidates_connection(
    provider: MT5MarketDataProvider, client: FakeMT5, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = RuntimeError("synthetic health failure")

    def fail_terminal_info() -> Record:
        raise failure

    monkeypatch.setattr(client, "terminal_info", fail_terminal_info)

    with pytest.raises(MT5Error, match="terminal_info failed") as error:
        provider.health()

    assert error.value.__cause__ is failure
    with pytest.raises(MT5Error, match="not initialized"):
        provider.latest_quote(SYMBOL)


def test_health_malformed_response_fails_closed(
    provider: MT5MarketDataProvider, client: FakeMT5, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "terminal_info", lambda: Record(connected="yes"))

    with pytest.raises(MT5Error, match="missing connected status"):
        provider.health()

    with pytest.raises(MT5Error, match="not initialized"):
        provider.latest_quote(SYMBOL)


def test_health_malformed_version_invalidates_connection(
    provider: MT5MarketDataProvider, client: FakeMT5, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "version", lambda: (500, "123", "test"))

    with pytest.raises(MT5Error, match="malformed terminal version"):
        provider.health()

    with pytest.raises(MT5Error, match="not initialized"):
        provider.latest_quote(SYMBOL)


@pytest.mark.parametrize(
    "method_name",
    (
        "symbols_get",
        "symbol_info",
        "copy_rates_range",
        "copy_ticks_range",
        "symbol_info_tick",
        "terminal_info",
        "version",
        "initialize",
        "shutdown",
    ),
)
def test_unexpected_client_method_exceptions_are_normalized_and_chained(
    method_name: str,
) -> None:
    client = FakeMT5()
    provider = MT5MarketDataProvider(client, SYMBOL, clock=lambda: NOW)
    if method_name != "initialize":
        provider.connect()

    failure = RuntimeError(f"synthetic {method_name} failure")

    def fail(*args: object, **kwargs: object) -> object:
        raise failure

    setattr(client, method_name, fail)
    operations = {
        "symbols_get": lambda: provider.discover_symbols(),
        "symbol_info": lambda: provider.symbol_info(SYMBOL),
        "copy_rates_range": lambda: provider.get_bars(SYMBOL, "M5", BAR_TIME, NOW),
        "copy_ticks_range": lambda: provider.get_ticks(SYMBOL, BAR_TIME, NOW),
        "symbol_info_tick": lambda: provider.latest_quote(SYMBOL),
        "terminal_info": lambda: provider.health(),
        "version": lambda: provider.health(),
        "initialize": lambda: provider.connect(),
        "shutdown": lambda: provider.shutdown(),
    }

    with pytest.raises(MT5Error, match=method_name) as error:
        operations[method_name]()

    assert error.value.__cause__ is failure
    if method_name in {"terminal_info", "version", "initialize", "shutdown"}:
        with pytest.raises(MT5Error, match="not initialized"):
            provider.latest_quote(SYMBOL)
    else:
        assert provider.health().status is DataStatus.VALID


def test_last_error_exception_preserves_cause_chain(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.rates = None
    failure = RuntimeError("synthetic last_error failure")

    def fail_last_error() -> tuple[int, str]:
        raise failure

    client.last_error = fail_last_error
    with pytest.raises(MT5Error, match="copy_rates_range failed.*last_error") as error:
        provider.get_bars(SYMBOL, "M5", BAR_TIME, NOW)

    assert isinstance(error.value.__cause__, MT5Error)
    assert error.value.__cause__.__cause__ is failure
    assert provider.health().status is DataStatus.VALID


@pytest.mark.parametrize("response", (None, (-123,), ("bad-code", "message")))
def test_last_error_malformed_response_is_reported(response: object) -> None:
    client = FakeMT5()
    provider = MT5MarketDataProvider(client, SYMBOL, clock=lambda: NOW)
    provider.connect()
    client.rates = None
    client.last_error = lambda: response  # type: ignore[assignment]

    with pytest.raises(MT5Error, match="copy_rates_range failed.*malformed response"):
        provider.get_bars(SYMBOL, "M5", BAR_TIME, NOW)

    assert provider.health().status is DataStatus.VALID


@pytest.mark.parametrize(
    ("method_name", "invoke"),
    (
        ("symbols_get", lambda provider: provider.discover_symbols()),
        ("copy_rates_range", lambda provider: provider.get_bars(SYMBOL, "M5", BAR_TIME, NOW)),
        ("copy_ticks_range", lambda provider: provider.get_ticks(SYMBOL, BAR_TIME, NOW)),
    ),
)
def test_non_iterable_collections_are_invalid_provider_responses(
    method_name: str, invoke: Callable[[MT5MarketDataProvider], object]
) -> None:
    client = FakeMT5()
    provider = MT5MarketDataProvider(client, SYMBOL, clock=lambda: NOW)
    provider.connect()

    def return_non_iterable(*args: object) -> object:
        return 42

    setattr(client, method_name, return_non_iterable)
    with pytest.raises(MT5Error, match=f"{method_name} returned an invalid"):
        invoke(provider)

    assert provider.health().status is DataStatus.VALID


@pytest.mark.parametrize(
    ("record_kind", "message"),
    (
        ("missing", "without usable _asdict"),
        ("non-mapping", "non-mapping"),
    ),
)
def test_malformed_symbol_discovery_records_are_rejected(record_kind: str, message: str) -> None:
    if record_kind == "missing":
        record: object = object()
    else:

        class NonMappingRecord:
            def _asdict(self) -> object:
                return []

        record = NonMappingRecord()

    client = FakeMT5()
    provider = MT5MarketDataProvider(client, SYMBOL, clock=lambda: NOW)
    provider.connect()
    client.symbols_get = lambda: (record,)  # type: ignore[assignment]

    with pytest.raises(MT5Error, match=f"symbols_get.*{message}"):
        provider.discover_symbols()

    assert provider.health().status is DataStatus.VALID


def test_symbol_record_conversion_exception_is_chained() -> None:
    failure = RuntimeError("synthetic _asdict conversion failure")

    class BrokenRecord:
        def _asdict(self) -> dict[str, object]:
            raise failure

    client = FakeMT5()
    provider = MT5MarketDataProvider(client, SYMBOL, clock=lambda: NOW)
    provider.connect()
    client.symbol_info = lambda symbol: BrokenRecord()  # type: ignore[assignment]

    with pytest.raises(MT5Error, match="symbol_info record _asdict\\(\\) failed") as error:
        provider.symbol_info(SYMBOL)

    assert error.value.__cause__ is failure
    assert provider.health().status is DataStatus.VALID


@pytest.mark.parametrize(
    ("row", "field"),
    (
        ({"time": int(BAR_TIME.timestamp()), "high": 102, "low": 99, "close": 101}, "open"),
        (object(), "response field time"),
    ),
)
def test_malformed_bar_rows_are_rejected_without_invalidating_connection(
    provider: MT5MarketDataProvider, client: FakeMT5, row: object, field: str
) -> None:
    client.rates = [row]  # type: ignore[list-item]

    with pytest.raises(MT5Error, match=f"copy_rates_range.*{field}"):
        provider.get_bars(SYMBOL, "M5", BAR_TIME, NOW)

    assert provider.health().status is DataStatus.VALID


def test_bar_row_conversion_exception_is_chained(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    failure = RuntimeError("synthetic row conversion failure")

    class BrokenRow:
        def __getitem__(self, field: str) -> object:
            raise failure

    client.rates = [BrokenRow()]  # type: ignore[list-item]
    with pytest.raises(MT5Error, match="copy_rates_range returned malformed bar") as error:
        provider.get_bars(SYMBOL, "M5", BAR_TIME, NOW)

    assert error.value.__cause__ is not None
    assert error.value.__cause__.__cause__ is failure
    assert provider.health().status is DataStatus.VALID


@pytest.mark.parametrize(
    ("row", "field"),
    (
        ({"bid": 100.0}, "time"),
        (object(), "response field time"),
    ),
)
def test_malformed_tick_rows_are_rejected_without_invalidating_connection(
    provider: MT5MarketDataProvider, client: FakeMT5, row: object, field: str
) -> None:
    client.ticks = [row]  # type: ignore[list-item]

    with pytest.raises(MT5Error, match=f"copy_ticks_range.*{field}"):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)

    assert provider.health().status is DataStatus.VALID


@pytest.mark.parametrize(
    ("record_kind", "message"),
    (
        ("missing", "without usable _asdict"),
        ("non-mapping", "non-mapping"),
    ),
)
def test_malformed_latest_quote_records_are_rejected(
    provider: MT5MarketDataProvider, client: FakeMT5, record_kind: str, message: str
) -> None:
    if record_kind == "missing":
        client.tick_info = object()  # type: ignore[assignment]
    else:

        class NonMappingRecord:
            def _asdict(self) -> object:
                return []

        client.tick_info = NonMappingRecord()  # type: ignore[assignment]

    with pytest.raises(MT5Error, match=f"symbol_info_tick.*{message}"):
        provider.latest_quote(SYMBOL)

    assert provider.health().status is DataStatus.VALID


def test_quote_record_conversion_exception_is_chained(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    failure = RuntimeError("synthetic quote conversion failure")

    class BrokenRecord:
        def _asdict(self) -> dict[str, object]:
            raise failure

    client.tick_info = BrokenRecord()  # type: ignore[assignment]
    with pytest.raises(MT5Error, match="symbol_info_tick record _asdict\\(\\) failed") as error:
        provider.latest_quote(SYMBOL)

    assert error.value.__cause__ is failure
    assert provider.health().status is DataStatus.VALID


def test_detected_disconnection_requires_explicit_connect_to_recover(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.terminal_connected = False

    report = provider.health()

    assert report.status is DataStatus.UNAVAILABLE
    assert report.detail == "Terminal disconnected"
    assert client.client_initialized
    with pytest.raises(MT5Error, match="not initialized"):
        provider.latest_quote(SYMBOL)
    assert client.initialized == 1

    provider.shutdown()

    assert client.shutdown_calls == 1
    assert not client.client_initialized
    with pytest.raises(MT5Error, match="not initialized"):
        provider.latest_quote(SYMBOL)

    provider.connect()

    assert client.initialized == 2
    assert client.client_initialized
    assert provider.health().status is DataStatus.VALID


def test_health_exception_preserves_cleanup_obligation_for_shutdown(
    provider: MT5MarketDataProvider, client: FakeMT5, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = RuntimeError("synthetic health failure")

    def fail_terminal_info() -> Record:
        raise failure

    monkeypatch.setattr(client, "terminal_info", fail_terminal_info)
    with pytest.raises(MT5Error, match="terminal_info failed") as error:
        provider.health()

    assert error.value.__cause__ is failure
    assert client.client_initialized
    with pytest.raises(MT5Error, match="not initialized"):
        provider.latest_quote(SYMBOL)

    provider.shutdown()

    assert client.shutdown_calls == 1
    assert not client.client_initialized


def test_connect_reconciles_old_initialization_before_reinitializing(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.terminal_connected = False
    assert provider.health().status is DataStatus.UNAVAILABLE

    client.terminal_connected = True
    provider.connect()

    assert client.shutdown_calls == 1
    assert client.initialized == 2
    assert client.client_initialized
    assert provider.health().status is DataStatus.VALID
    assert provider.latest_quote(SYMBOL).instrument == SYMBOL


def test_shutdown_exception_invalidates_connection_until_explicit_connect(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    failure = RuntimeError("synthetic shutdown failure")
    client.shutdown_exception = failure

    with pytest.raises(MT5Error, match="shutdown failed") as error:
        provider.shutdown()

    assert error.value.__cause__ is failure
    assert client.client_initialized
    assert client.initialized == 1
    assert provider.health().status is DataStatus.UNAVAILABLE
    with pytest.raises(MT5Error, match="not initialized"):
        provider.latest_quote(SYMBOL)

    with pytest.raises(MT5Error, match="shutdown failed"):
        provider.connect()
    assert client.initialized == 1
    assert client.shutdown_calls == 2
    assert client.client_initialized

    client.shutdown_exception = None
    provider.connect()

    assert client.shutdown_calls == 3
    assert client.initialized == 2
    assert client.client_initialized
    assert provider.health().status is DataStatus.VALID
    assert provider.latest_quote(SYMBOL).instrument == SYMBOL


def test_health_disconnection_blocks_all_data_operations(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.terminal_connected = False
    assert provider.health().status is DataStatus.UNAVAILABLE

    operations = (
        lambda: provider.discover_symbols(),
        lambda: provider.symbol_info(SYMBOL),
        lambda: provider.get_bars(SYMBOL, "M5", BAR_TIME, NOW),
        lambda: provider.get_ticks(SYMBOL, BAR_TIME, NOW),
        lambda: provider.latest_quote(SYMBOL),
    )
    for operation in operations:
        with pytest.raises(MT5Error, match="not initialized"):
            operation()
    assert client.rate_args is None
    assert client.tick_args is None


def test_discovery_does_not_choose_symbol(provider: MT5MarketDataProvider) -> None:
    candidates = provider.discover_symbols()
    assert [item.symbol for item in candidates] == [SYMBOL, "GOLD"]
    assert candidates[0].contract_size == 100
    assert candidates[0].currency_profit == "USD"
    with pytest.raises(MT5Error, match="explicitly select|Explicitly select"):
        provider.latest_quote("GOLD")
    with pytest.raises(MT5Error, match="symbol_info failed"):
        provider.symbol_info("NO_SUCH_SYMBOL")


@pytest.mark.parametrize(
    ("timeframe", "constant"),
    [("M1", 101), ("M5", 105), ("M15", 115), ("H1", 201), ("H4", 204), ("D1", 301)],
)
def test_bars_use_injected_constants_and_utc_ranges(
    provider: MT5MarketDataProvider, client: FakeMT5, timeframe: str, constant: int
) -> None:
    shared_provider: MarketDataProvider = provider
    start = BAR_TIME.astimezone(timezone(timedelta(hours=2)))
    bars = shared_provider.get_bars(SYMBOL, timeframe, start, NOW - timedelta(days=1))
    assert client.rate_args == (SYMBOL, constant, BAR_TIME, NOW - timedelta(days=1))
    assert bars[0].timestamp_utc == BAR_TIME
    assert bars[0].tick_volume == 17
    assert bars[0].real_volume == 7
    assert bars[0].volume is None
    assert bars[0].spread_points == 3


def test_ticks_and_latest_quote(provider: MT5MarketDataProvider, client: FakeMT5) -> None:
    ticks = provider.get_ticks(SYMBOL, BAR_TIME, NOW - timedelta(days=1))
    assert client.tick_args == (SYMBOL, BAR_TIME, NOW - timedelta(days=1), 999)
    assert ticks[0].timestamp_milliseconds == int(BAR_TIME.timestamp() * 1000)
    assert ticks[0].bid == 100
    quote = provider.latest_quote(SYMBOL)
    assert quote.bid == 100
    assert quote.ask == 100.2
    assert quote.retrieved_at_utc == NOW


def test_tick_milliseconds_are_preserved_and_consistent(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.ticks[0]["time_msc"] += 500
    ticks = provider.get_ticks(SYMBOL, BAR_TIME + timedelta(milliseconds=400), NOW)
    assert ticks[0].timestamp_utc == BAR_TIME + timedelta(milliseconds=500)
    client.ticks[0]["time_msc"] += 1000
    with pytest.raises(MT5Error, match="seconds and milliseconds"):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)


def test_historical_ticks_accept_bid_and_ask_updates_with_carried_fields(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.ticks = [
        _tick_row(flags=134, bid=100.1, ask=101.0),
        _tick_row(
            BAR_TIME + timedelta(seconds=1),
            flags=4,
            bid=100.1,
            ask=101.2,
        ),
    ]  # type: ignore[list-item]

    ticks = provider.get_ticks(SYMBOL, BAR_TIME, NOW)

    assert [tick.bid for tick in ticks] == [100.1, 100.1]
    assert [tick.ask for tick in ticks] == [101.0, 101.2]
    assert [tick.flags for tick in ticks] == [134, 4]
    assert ticks[0].timestamp_utc < ticks[1].timestamp_utc
    assert provider.health().status is DataStatus.VALID


def test_historical_trade_tick_accepts_last_and_volume_changes(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.ticks = [
        _tick_row(
            flags=24,
            last=105.5,
            volume=3,
            volume_real=3.25,
        )
    ]  # type: ignore[list-item]

    tick = provider.get_ticks(SYMBOL, BAR_TIME, NOW)[0]

    assert tick.last == 105.5
    assert tick.volume == 3.25
    assert tick.flags == 24


def test_historical_tick_preserves_integer_volume_without_real_volume(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    row = _tick_row(flags=16, volume=3)
    row.pop("volume_real")
    client.ticks = [row]  # type: ignore[list-item]

    tick = provider.get_ticks(SYMBOL, BAR_TIME, NOW)[0]

    assert tick.volume == 3.0


def test_historical_tick_preserves_zero_real_volume(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.ticks = [_tick_row(flags=16, volume=3, volume_real=0.0)]  # type: ignore[list-item]

    tick = provider.get_ticks(SYMBOL, BAR_TIME, NOW)[0]

    assert tick.volume == 0.0


def test_historical_information_tick_allows_zero_last(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.ticks = [_tick_row(flags=8, last=0.0)]  # type: ignore[list-item]

    tick = provider.get_ticks(SYMBOL, BAR_TIME, NOW)[0]

    assert tick.last == 0.0
    assert tick.flags == 8
    assert provider.health().status is DataStatus.VALID


def test_historical_ticks_preserve_documented_empty_order_book_event(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    row = _tick_row(flags=6, bid=0.0, ask=0.0)
    client.ticks = [row]  # type: ignore[list-item]
    client.tick_info = Record(**row)

    tick = provider.get_ticks(SYMBOL, BAR_TIME, NOW)[0]

    assert (tick.bid, tick.ask, tick.flags) == (0.0, 0.0, 6)
    with pytest.raises(MT5Error, match="Invalid bid/ask quote"):
        provider.latest_quote(SYMBOL)
    assert provider.health().status is DataStatus.VALID


def test_historical_ticks_reject_empty_book_with_unrelated_flag_bits(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.ticks = [_tick_row(flags=134, bid=0.0, ask=0.0)]  # type: ignore[list-item]

    with pytest.raises(MT5Error, match="zero requires a flagged empty book"):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)

    assert provider.health().status is DataStatus.VALID


@pytest.mark.parametrize(
    ("field", "value"),
    (("volume", -1), ("volume_real", -0.1)),
)
def test_historical_ticks_reject_negative_volume_without_invalidating_health(
    provider: MT5MarketDataProvider, client: FakeMT5, field: str, value: object
) -> None:
    client.ticks = [_tick_row(flags=16, **{field: value})]  # type: ignore[list-item]

    with pytest.raises(MT5Error, match="volume"):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)

    assert provider.health().status is DataStatus.VALID


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("bid", float("nan")),
        ("ask", float("inf")),
        ("last", float("-inf")),
        ("volume_real", float("nan")),
    ),
)
def test_historical_ticks_reject_non_finite_values(
    provider: MT5MarketDataProvider, client: FakeMT5, field: str, value: float
) -> None:
    client.ticks = [_tick_row(**{field: value})]  # type: ignore[list-item]

    with pytest.raises(MT5Error, match="finite|Invalid"):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)

    assert provider.health().status is DataStatus.VALID


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("time", float("nan")),
        ("time", -1),
        ("time_msc", -1),
        ("time_msc", int(BAR_TIME.timestamp() * 1000) + 1000),
    ),
)
def test_historical_ticks_reject_invalid_timestamp_values(
    provider: MT5MarketDataProvider, client: FakeMT5, field: str, value: object
) -> None:
    client.ticks = [_tick_row(**{field: value})]  # type: ignore[list-item]

    with pytest.raises(MT5Error, match="timestamp|seconds and milliseconds|Non-finite"):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)

    assert provider.health().status is DataStatus.VALID


@pytest.mark.parametrize("flags", (True, -1, 1 << 32, 2.0))
def test_historical_ticks_reject_invalid_flag_type_or_range(
    provider: MT5MarketDataProvider, client: FakeMT5, flags: object
) -> None:
    client.ticks = [_tick_row(flags=flags)]  # type: ignore[list-item]

    with pytest.raises(MT5Error, match="tick flags"):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)

    assert provider.health().status is DataStatus.VALID


def test_historical_ticks_allow_missing_optional_fields_when_unchanged(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    row = _tick_row()
    row.pop("last")
    row.pop("volume")
    row.pop("volume_real")
    client.ticks = [row]  # type: ignore[list-item]

    tick = provider.get_ticks(SYMBOL, BAR_TIME, NOW)[0]

    assert tick.last is None
    assert tick.volume is None
    assert provider.health().status is DataStatus.VALID


def test_historical_ticks_require_values_for_fields_marked_changed(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    row = _tick_row(flags=8)
    row.pop("last")
    client.ticks = [row]  # type: ignore[list-item]

    with pytest.raises(MT5Error, match="LAST flag.*missing"):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)

    assert provider.health().status is DataStatus.VALID


def test_historical_ticks_reject_out_of_order_records(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.ticks = [
        _tick_row(BAR_TIME + timedelta(seconds=1), flags=2),
        _tick_row(BAR_TIME, flags=4),
    ]  # type: ignore[list-item]

    with pytest.raises(MT5Error, match="chronological order"):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)

    assert provider.health().status is DataStatus.VALID


@pytest.mark.parametrize(
    ("bid", "ask", "message"),
    ((-0.1, 100.0, "negative"), (100.1, 100.0, "below bid")),
)
def test_historical_ticks_reject_negative_or_crossed_prices(
    provider: MT5MarketDataProvider,
    client: FakeMT5,
    bid: float,
    ask: float,
    message: str,
) -> None:
    client.ticks = [_tick_row(bid=bid, ask=ask)]  # type: ignore[list-item]

    with pytest.raises(MT5Error, match=message):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)

    assert provider.health().status is DataStatus.VALID


def test_historical_ticks_reject_exact_duplicate_records(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.ticks = [_tick_row(), _tick_row()]  # type: ignore[list-item]

    with pytest.raises(MT5Error, match="duplicate tick"):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)

    assert provider.health().status is DataStatus.VALID


def test_historical_ticks_preserve_distinct_updates_with_equal_timestamps(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.ticks = [
        _tick_row(bid=100.0),
        _tick_row(bid=100.1),
    ]  # type: ignore[list-item]

    ticks = provider.get_ticks(SYMBOL, BAR_TIME, NOW)

    assert len(ticks) == 2
    assert [tick.bid for tick in ticks] == [100.0, 100.1]


@pytest.mark.parametrize(
    ("record", "message"),
    (
        (Record(time=int(BAR_TIME.timestamp()), bid=101, ask=100, flags=2), "Invalid bid/ask"),
        (Record(time=int(BAR_TIME.timestamp()), ask=101, flags=2), "bid"),
        (
            Record(
                time=int(BAR_TIME.timestamp()),
                bid=100,
                ask=101,
                time_msc=int(BAR_TIME.timestamp() * 1000),
            ),
            "tick flags",
        ),
    ),
)
def test_latest_quote_rejects_crossed_or_missing_snapshot_fields(
    provider: MT5MarketDataProvider, client: FakeMT5, record: Record, message: str
) -> None:
    client.tick_info = record

    with pytest.raises(MT5Error, match=message):
        provider.latest_quote(SYMBOL)

    assert provider.health().status is DataStatus.VALID


def test_optional_symbol_fields_and_invalid_specifications(
    provider: MT5MarketDataProvider, client: FakeMT5, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "symbol_info", lambda symbol: Record(name=symbol))
    assert provider.symbol_info(SYMBOL).tick_size is None
    monkeypatch.setattr(client, "symbol_info", lambda symbol: Record(name=symbol, point=-1))
    with pytest.raises(MT5Error, match="point must be positive"):
        provider.symbol_info(SYMBOL)
    monkeypatch.setattr(
        client, "symbol_info", lambda symbol: Record(name=symbol, volume_min=2, volume_max=1)
    )
    with pytest.raises(MT5Error, match="volume_min exceeds volume_max"):
        provider.symbol_info(SYMBOL)


def test_invalid_quote_and_missing_tick_fields(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.tick_info = Record(time=int(BAR_TIME.timestamp()), bid=101, ask=100, flags=2)
    with pytest.raises(MT5Error, match="Invalid bid/ask"):
        provider.latest_quote(SYMBOL)
    client.tick_info = Record(time=int(BAR_TIME.timestamp()), bid=100, flags=2)
    with pytest.raises(MT5Error, match="numeric field: ask"):
        provider.latest_quote(SYMBOL)


def test_none_and_empty_history_are_not_successful_bars(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    client.rates = None
    with pytest.raises(MT5Error, match=r"copy_rates_range failed.*-123"):
        provider.get_bars(SYMBOL, "M5", BAR_TIME, NOW)
    client.rates = []
    with pytest.raises(MT5Error, match="EMPTY_HISTORY"):
        provider.get_bars(SYMBOL, "M5", BAR_TIME, NOW)
    client.ticks = None
    with pytest.raises(MT5Error, match=r"copy_ticks_range failed.*-123"):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)
    client.ticks = []
    with pytest.raises(MT5EmptyHistoryError, match="No ticks returned"):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)
    client.tick_info = None
    with pytest.raises(MT5Error, match=r"symbol_info_tick failed.*-123"):
        provider.latest_quote(SYMBOL)


def test_latest_complete_d1_distinguishes_empty_history_from_terminal_failure(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    calendar = SessionCalendar(
        calendar_id="synthetic.mt5",
        version="synthetic-v1",
        provider_id="mt5",
        instrument_id=SYMBOL,
        provenance=CalendarProvenance("synthetic-test-fixture", "tests/test_mt5_adapter.py"),
        timezone_name="UTC",
        effective_start_utc=BAR_TIME - timedelta(days=1),
        effective_end_utc=NOW + timedelta(days=1),
        coverage_start_utc=BAR_TIME - timedelta(days=1),
        coverage_end_utc=NOW + timedelta(days=1),
        weekly_sessions=(),
    )
    pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)
    client.rates = []
    assert pipeline.latest_complete_bar(SYMBOL, "D1", calendar=calendar, lookback_days=3) is None

    client.rates = None
    with pytest.raises(MT5Error, match=r"copy_rates_range failed.*-123"):
        pipeline.latest_complete_bar(SYMBOL, "D1", calendar=calendar, lookback_days=3)


def test_range_validation_and_closed_bar_guard(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        provider.get_bars(SYMBOL, "M5", datetime(2026, 1, 1), NOW)
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        provider.get_bars(SYMBOL, "M30", BAR_TIME, NOW)
    client.rates[0]["time"] = int((NOW - timedelta(minutes=1)).timestamp())
    with pytest.raises(MT5Error, match="INCOMPLETE_BAR"):
        provider.get_bars(SYMBOL, "M5", NOW - timedelta(minutes=5), NOW)


def test_mt5_gap_reaches_pipeline_calendar_and_known_closure_is_accepted(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    second = dict(client.rates[0])
    second["time"] += 10 * 60
    client.rates.append(second)
    raw = provider.get_bars(SYMBOL, "M5", BAR_TIME, NOW - timedelta(minutes=5))
    assert len(raw) == 2

    calendar = SessionCalendar(
        calendar_id="synthetic.mt5",
        version="synthetic-v1",
        provider_id="mt5",
        instrument_id=SYMBOL,
        provenance=CalendarProvenance("synthetic-test-fixture", "tests/test_mt5_adapter.py"),
        timezone_name="UTC",
        effective_start_utc=BAR_TIME - timedelta(days=1),
        effective_end_utc=NOW,
        coverage_start_utc=BAR_TIME - timedelta(days=1),
        coverage_end_utc=NOW,
        weekly_sessions=(),
    )
    pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)
    validated = pipeline.retrieve_validated_bars(
        SYMBOL, "M5", NOW, history_days=3, calendar=calendar
    )

    assert validated.report.status is DataStatus.VALID
    assert "KNOWN_MARKET_CLOSURE" in validated.report.reasons
    assert validated.calendar_id == calendar.calendar_id
    assert validated.calendar_version == calendar.version


def test_mt5_gap_during_open_session_and_unknown_calendar_fail_closed(
    provider: MT5MarketDataProvider, client: FakeMT5
) -> None:
    second = dict(client.rates[0])
    second["time"] += 10 * 60
    client.rates.append(second)
    pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)
    open_calendar = SessionCalendar(
        calendar_id="synthetic.mt5",
        version="open-v1",
        provider_id="mt5",
        instrument_id=SYMBOL,
        provenance=CalendarProvenance("synthetic-test-fixture", "tests/test_mt5_adapter.py"),
        timezone_name="UTC",
        effective_start_utc=BAR_TIME - timedelta(days=1),
        effective_end_utc=NOW,
        coverage_start_utc=BAR_TIME - timedelta(days=1),
        coverage_end_utc=NOW,
        weekly_sessions=(WeeklySession(BAR_TIME.weekday(), time(0), time(23, 59)),),
    )
    with pytest.raises(HistoricalDataQualityError, match="MISSING_BAR"):
        pipeline.retrieve_validated_bars(SYMBOL, "M5", NOW, history_days=3, calendar=open_calendar)

    unknown_calendar = SessionCalendar(
        calendar_id="synthetic.mt5",
        version="unknown-v1",
        provider_id="mt5",
        instrument_id=SYMBOL,
        provenance=CalendarProvenance("synthetic-test-fixture", "tests/test_mt5_adapter.py"),
        timezone_name="UTC",
        effective_start_utc=BAR_TIME - timedelta(days=1),
        effective_end_utc=NOW,
        coverage_start_utc=BAR_TIME + timedelta(minutes=6),
        coverage_end_utc=NOW,
        weekly_sessions=(),
    )
    with pytest.raises(HistoricalDataQualityError, match="CALENDAR_COVERAGE_UNKNOWN"):
        pipeline.retrieve_validated_bars(
            SYMBOL, "M5", NOW, history_days=3, calendar=unknown_calendar
        )


@pytest.mark.parametrize(
    ("provider_id", "instrument_id", "message"),
    [
        ("synthetic-other-provider", SYMBOL, "provider/source identity"),
        ("mt5", "synthetic-other-instrument", "instrument identity"),
    ],
)
def test_pipeline_and_direct_validation_reject_calendar_scope_mismatch(
    provider: MT5MarketDataProvider,
    client: FakeMT5,
    provider_id: str,
    instrument_id: str,
    message: str,
) -> None:
    second = dict(client.rates[0])
    second["time"] += 10 * 60
    client.rates.append(second)
    closure_start = BAR_TIME + timedelta(minutes=5)
    calendar = SessionCalendar(
        calendar_id="synthetic.mt5",
        version="scope-mismatch",
        provider_id=provider_id,
        instrument_id=instrument_id,
        provenance=CalendarProvenance("synthetic-test-fixture", "tests/test_mt5_adapter.py"),
        timezone_name="UTC",
        effective_start_utc=BAR_TIME - timedelta(days=1),
        effective_end_utc=NOW,
        coverage_start_utc=BAR_TIME - timedelta(days=1),
        coverage_end_utc=NOW,
        weekly_sessions=(),
        exceptional_closures=(
            CalendarClosure(
                closure_start,
                closure_start + timedelta(minutes=5),
                "synthetic closure must not bypass scope checks",
            ),
        ),
    )
    pipeline = HistoricalDataPipeline(provider, clock=lambda: NOW)

    with pytest.raises(ValueError, match=message):
        pipeline.retrieve_bars(SYMBOL, "M5", NOW, history_days=3, calendar=calendar)
    with pytest.raises(ValueError, match=message):
        pipeline.retrieve_validated_bars(SYMBOL, "M5", NOW, history_days=3, calendar=calendar)
    with pytest.raises(ValueError, match=message):
        pipeline.check_coverage(
            SYMBOL,
            "M5",
            BAR_TIME - timedelta(minutes=5),
            NOW - timedelta(minutes=5),
            calendar=calendar,
        )
    bars = provider.get_bars(
        SYMBOL, "M5", BAR_TIME - timedelta(minutes=5), NOW - timedelta(minutes=5)
    )
    with pytest.raises(ValueError, match=message):
        pipeline.validate_bars(bars, "M5", calendar=calendar)
    with pytest.raises(ValueError, match=message):
        validate_bars(bars, "M5", NOW, calendar=calendar)


def test_empty_history_checks_explicit_instrument_without_inferring_provider() -> None:
    calendar = SessionCalendar(
        calendar_id="synthetic.mt5",
        version="empty-history",
        provider_id="synthetic-provider-not-inferred",
        instrument_id=SYMBOL,
        provenance=CalendarProvenance("synthetic-test-fixture", "tests/test_mt5_adapter.py"),
        timezone_name="UTC",
        effective_start_utc=BAR_TIME - timedelta(days=1),
        effective_end_utc=NOW,
        coverage_start_utc=BAR_TIME - timedelta(days=1),
        coverage_end_utc=NOW,
        weekly_sessions=(),
    )
    pipeline = HistoricalDataPipeline(
        MT5MarketDataProvider(FakeMT5(), clock=lambda: NOW),
        clock=lambda: NOW,
    )
    report = pipeline.validate_bars((), "M5", calendar=calendar, expected_instrument=SYMBOL)

    assert report.status is DataStatus.INSUFFICIENT_DATA
    assert report.reasons == ("EMPTY_HISTORY",)
    with pytest.raises(ValueError, match="instrument identity"):
        pipeline.validate_bars((), "M5", calendar=calendar, expected_instrument="SYNTHETIC_OTHER")


@pytest.mark.parametrize(
    ("times", "changes", "message"),
    [
        ((BAR_TIME, BAR_TIME), ({}, {}), "DUPLICATE_BAR"),
        ((BAR_TIME + timedelta(minutes=5), BAR_TIME), ({}, {}), "OUT_OF_ORDER"),
        (
            (BAR_TIME, BAR_TIME + timedelta(minutes=5)),
            ({"high": 90.0}, {}),
            "Invalid OHLC data",
        ),
    ],
)
def test_mt5_intrinsic_bar_validation_still_fails_closed(
    provider: MT5MarketDataProvider,
    client: FakeMT5,
    times: tuple[datetime, datetime],
    changes: tuple[dict[str, float], dict[str, float]],
    message: str,
) -> None:
    original = dict(client.rates[0])
    client.rates[:] = []
    for timestamp, change in zip(times, changes, strict=True):
        row = dict(original)
        row["time"] = int(timestamp.timestamp())
        row.update(change)
        client.rates.append(row)

    with pytest.raises(MT5Error, match=message):
        provider.get_bars(SYMBOL, "M5", BAR_TIME - timedelta(minutes=5), NOW)


def test_safety_policy_allows_constants_but_rejects_trading_access() -> None:
    assert not analyze_source(
        "import MetaTrader5 as mt\nx = mt.TIMEFRAME_H4\ny = mt.COPY_TICKS_ALL",
        "adapters/mt5/provider.py",
    )
    assert analyze_source(
        "import MetaTrader5 as mt\nmt.order_send({})",
        "adapters/mt5/provider.py",
    )
    assert analyze_source(
        "import MetaTrader5 as mt\nmt.account_info()",
        "adapters/mt5/provider.py",
    )
    from pathlib import Path

    assert not scan_package(Path(__file__).parents[1] / "src" / "kankalar_gold_ai")
