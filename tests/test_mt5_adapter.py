"""Read-only MT5 tests: all terminal calls are handled by a deterministic fake."""

import sys
from datetime import datetime, timedelta, timezone

import pytest

from kankalar_gold_ai.adapters.mt5.client import load_windows_client
from kankalar_gold_ai.adapters.mt5.provider import MT5Error, MT5MarketDataProvider
from kankalar_gold_ai.core.contracts import DataStatus, MarketDataProvider
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
        self.shutdown_calls = 0
        self.initialized = 0
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

    def initialize(self) -> bool:
        self.initialized += 1
        return self.initialize_result

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def version(self) -> tuple[int, int, str]:
        return (500, 123, "test")

    def terminal_info(self) -> Record:
        return Record(connected=True)

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
    adapter.connect()
    adapter.connect()
    assert client.initialized == 1
    assert adapter.health().status == DataStatus.VALID
    assert adapter.health().terminal_version == "500.123.test"
    adapter.shutdown()
    adapter.shutdown()
    assert client.shutdown_calls == 1
    assert adapter.health().status == DataStatus.UNAVAILABLE


def test_initialization_failure_reports_last_error(client: FakeMT5) -> None:
    client.initialize_result = False
    with pytest.raises(MT5Error, match=r"initialize failed.*-123"):
        MT5MarketDataProvider(client).connect()


def test_health_failure_reports_terminal_error(
    provider: MT5MarketDataProvider, client: FakeMT5, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "terminal_info", lambda: None)
    with pytest.raises(MT5Error, match=r"terminal_info failed.*-123"):
        provider.health()


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
    client.tick_info = Record(time=int(BAR_TIME.timestamp()), bid=101, ask=100)
    with pytest.raises(MT5Error, match="Invalid bid/ask"):
        provider.latest_quote(SYMBOL)
    client.tick_info = Record(time=int(BAR_TIME.timestamp()), bid=100)
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
    with pytest.raises(MT5Error, match="No ticks returned"):
        provider.get_ticks(SYMBOL, BAR_TIME, NOW)
    client.tick_info = None
    with pytest.raises(MT5Error, match=r"symbol_info_tick failed.*-123"):
        provider.latest_quote(SYMBOL)


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
