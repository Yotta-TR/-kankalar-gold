"""Injected, read-only MT5 data access; no credentials or trading APIs."""

import math
from datetime import datetime, timedelta, timezone
from numbers import Real
from typing import Callable, Mapping

from kankalar_gold_ai.adapters.mt5.client import MT5ArrayRecord, MT5Client, MT5NamedRecord
from kankalar_gold_ai.core.contracts import (
    DataStatus,
    InstrumentSpecification,
    MarketBar,
    MarketDataHealth,
    MarketQuote,
    MarketTick,
    as_utc,
)
from kankalar_gold_ai.data.quality import TIMEFRAME_DURATIONS, validate_bars


class MT5Error(RuntimeError):
    """An explicit read-only terminal or data error."""


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise MT5Error(f"Missing or invalid numeric field: {field}")
    result = float(value)
    if not math.isfinite(result):
        raise MT5Error(f"Non-finite numeric field: {field}")
    return result


def _optional_number(values: Mapping[str, object], field: str) -> float | None:
    value = values.get(field)
    return None if value is None else _number(value, field)


def _optional_integer(values: Mapping[str, object], field: str) -> int | None:
    value = _optional_number(values, field)
    if value is None:
        return None
    if not value.is_integer():
        raise MT5Error(f"Expected integer field: {field}")
    return int(value)


def _optional_text(values: Mapping[str, object], field: str) -> str | None:
    value = values.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MT5Error(f"Invalid text field: {field}")
    return value


def _timestamp(seconds: object) -> datetime:
    value = _number(seconds, "time")
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (ValueError, OverflowError, OSError) as error:
        raise MT5Error("Invalid MT5 timestamp") from error


def _tick_timestamp(values: Mapping[str, object]) -> tuple[datetime, int | None]:
    timestamp = _timestamp(values.get("time"))
    milliseconds = _optional_integer(values, "time_msc")
    if milliseconds is None:
        return timestamp, None
    second = math.floor(timestamp.timestamp())
    if not second * 1000 <= milliseconds < (second + 1) * 1000:
        raise MT5Error("Tick seconds and milliseconds are inconsistent")
    return _timestamp(milliseconds / 1000), milliseconds


def _row_values(row: MT5ArrayRecord, fields: tuple[str, ...]) -> dict[str, object]:
    values: dict[str, object] = {}
    for field in fields:
        try:
            values[field] = row[field]
        except (KeyError, ValueError, IndexError):
            continue
    return values


class MT5MarketDataProvider:
    """Read-only adapter conforming to the existing market-bar provider contract."""

    def __init__(
        self,
        client: MT5Client,
        selected_symbol: str | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if selected_symbol is not None and not selected_symbol.strip():
            raise ValueError("selected_symbol must not be blank")
        self._client = client
        self._selected_symbol = selected_symbol
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._connected = False

    def _now(self) -> datetime:
        return as_utc(self._clock())

    def _failure(self, operation: str) -> MT5Error:
        code, message = self._client.last_error()
        return MT5Error(f"{operation} failed (MT5 {code}: {message})")

    def _require_connected(self) -> None:
        if not self._connected:
            raise MT5Error("MT5 terminal is not initialized")

    def _require_symbol(self, instrument: str) -> None:
        if self._selected_symbol is None or instrument != self._selected_symbol:
            raise MT5Error("Explicitly select the requested broker symbol first")

    def connect(self) -> None:
        if self._connected:
            return
        if not self._client.initialize():
            raise self._failure("initialize")
        self._connected = True

    def shutdown(self) -> None:
        if self._connected:
            self._client.shutdown()
            self._connected = False

    def health(self) -> MarketDataHealth:
        now = self._now()
        if not self._connected:
            return MarketDataHealth(DataStatus.UNAVAILABLE, "mt5", now, "Not initialized")
        info = self._client.terminal_info()
        if info is None:
            raise self._failure("terminal_info")
        version = self._client.version()
        if version is None:
            raise self._failure("version")
        connected = info._asdict().get("connected")
        if not isinstance(connected, bool):
            raise MT5Error("terminal_info is missing connected status")
        return MarketDataHealth(
            DataStatus.VALID if connected else DataStatus.UNAVAILABLE,
            "mt5",
            now,
            "Connected" if connected else "Terminal disconnected",
            ".".join(str(item) for item in version),
        )

    def discover_symbols(self) -> tuple[InstrumentSpecification, ...]:
        self._require_connected()
        symbols = self._client.symbols_get()
        if symbols is None:
            raise self._failure("symbols_get")
        candidates: list[InstrumentSpecification] = []
        for symbol in symbols:
            name = _optional_text(symbol._asdict(), "name")
            if name is None:
                raise MT5Error("Symbol record is missing name")
            if "XAUUSD" in name.upper() or "GOLD" in name.upper():
                candidates.append(self.symbol_info(name))
        return tuple(candidates)

    def symbol_info(self, symbol: str) -> InstrumentSpecification:
        self._require_connected()
        if not symbol.strip():
            raise ValueError("symbol must not be blank")
        info = self._client.symbol_info(symbol)
        if info is None:
            raise self._failure("symbol_info")
        values = info._asdict()
        if _optional_text(values, "name") != symbol:
            raise MT5Error("symbol_info returned a different or missing symbol name")
        specification = InstrumentSpecification(
            symbol=symbol,
            source="mt5",
            retrieved_at_utc=self._now(),
            digits=_optional_integer(values, "digits"),
            point=_optional_number(values, "point"),
            contract_size=_optional_number(values, "trade_contract_size"),
            tick_size=_optional_number(values, "trade_tick_size"),
            tick_value=_optional_number(values, "trade_tick_value"),
            volume_min=_optional_number(values, "volume_min"),
            volume_max=_optional_number(values, "volume_max"),
            volume_step=_optional_number(values, "volume_step"),
            trade_mode=_optional_integer(values, "trade_mode"),
            stops_level=_optional_integer(values, "trade_stops_level"),
            freeze_level=_optional_integer(values, "trade_freeze_level"),
            currency_profit=_optional_text(values, "currency_profit"),
            currency_margin=_optional_text(values, "currency_margin"),
        )
        for field in (
            "point",
            "contract_size",
            "tick_size",
            "volume_min",
            "volume_max",
            "volume_step",
        ):
            value = values.get(
                {"contract_size": "trade_contract_size", "tick_size": "trade_tick_size"}.get(
                    field, field
                )
            )
            if value is not None and _number(value, field) <= 0:
                raise MT5Error(f"Invalid symbol specification: {field} must be positive")
        if (
            specification.volume_min is not None
            and specification.volume_max is not None
            and specification.volume_min > specification.volume_max
        ):
            raise MT5Error("Invalid symbol specification: volume_min exceeds volume_max")
        if specification.digits is not None and specification.digits < 0:
            raise MT5Error("Invalid symbol specification: digits must be non-negative")
        return specification

    def _timeframe(self, timeframe: str) -> int:
        mapping = {
            "M1": self._client.TIMEFRAME_M1,
            "M5": self._client.TIMEFRAME_M5,
            "M15": self._client.TIMEFRAME_M15,
            "H1": self._client.TIMEFRAME_H1,
            "H4": self._client.TIMEFRAME_H4,
            "D1": self._client.TIMEFRAME_D1,
        }
        if timeframe not in mapping:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        return mapping[timeframe]

    def _range(self, instrument: str, start: datetime, end: datetime) -> tuple[datetime, datetime]:
        self._require_connected()
        self._require_symbol(instrument)
        start, end = as_utc(start), as_utc(end)
        if start > end:
            raise ValueError("start must not be after end")
        if end - start > timedelta(days=366):
            raise ValueError("Requested range exceeds 366 days")
        return start, end

    def get_bars(
        self, instrument: str, timeframe: str, start_utc: datetime, end_utc: datetime
    ) -> tuple[MarketBar, ...]:
        start, end = self._range(instrument, start_utc, end_utc)
        frame = self._timeframe(timeframe)
        rows = self._client.copy_rates_range(instrument, frame, start, end)
        if rows is None:
            raise self._failure("copy_rates_range")
        now = self._now()
        bars: list[MarketBar] = []
        fields = ("time", "open", "high", "low", "close", "tick_volume", "real_volume", "spread")
        for row in rows:
            values = _row_values(row, fields)
            timestamp = _timestamp(values.get("time"))
            if not start <= timestamp <= end:
                raise MT5Error("MT5 returned a bar outside the requested range")
            try:
                bars.append(
                    MarketBar(
                        instrument=instrument,
                        timeframe=timeframe,
                        timestamp_utc=timestamp,
                        open=_number(values.get("open"), "open"),
                        high=_number(values.get("high"), "high"),
                        low=_number(values.get("low"), "low"),
                        close=_number(values.get("close"), "close"),
                        volume=None,
                        source="mt5",
                        tick_volume=_optional_integer(values, "tick_volume"),
                        real_volume=_optional_integer(values, "real_volume"),
                        spread_points=_optional_integer(values, "spread"),
                        retrieved_at_utc=now,
                    )
                )
            except ValueError as error:
                raise MT5Error(f"Invalid OHLC data: {error}") from error
        result = tuple(bars)
        report = validate_bars(result, timeframe, now)
        if report.status != DataStatus.VALID:
            raise MT5Error(f"Bar quality {report.status}: {', '.join(report.reasons)}")
        if result[-1].timestamp_utc + TIMEFRAME_DURATIONS[timeframe] > now:
            raise MT5Error("Incomplete bar")
        return result

    def get_ticks(
        self, instrument: str, start_utc: datetime, end_utc: datetime
    ) -> tuple[MarketTick, ...]:
        start, end = self._range(instrument, start_utc, end_utc)
        rows = self._client.copy_ticks_range(instrument, start, end, self._client.COPY_TICKS_ALL)
        if rows is None:
            raise self._failure("copy_ticks_range")
        if len(rows) == 0:
            raise MT5Error("No ticks returned; historical coverage is unavailable")
        now = self._now()
        fields = ("time", "time_msc", "bid", "ask", "last", "volume_real", "flags")
        ticks = []
        for row in rows:
            values = _row_values(row, fields)
            timestamp, milliseconds = _tick_timestamp(values)
            if not start <= timestamp <= end:
                raise MT5Error("MT5 returned a tick outside the requested range")
            ticks.append(
                MarketTick(
                    instrument=instrument,
                    timestamp_utc=timestamp,
                    timestamp_milliseconds=milliseconds,
                    bid=_optional_number(values, "bid"),
                    ask=_optional_number(values, "ask"),
                    last=_optional_number(values, "last"),
                    volume=_optional_number(values, "volume_real"),
                    flags=_optional_integer(values, "flags"),
                    source="mt5",
                    retrieved_at_utc=now,
                )
            )
        return tuple(ticks)

    def latest_quote(self, instrument: str) -> MarketQuote:
        self._require_connected()
        self._require_symbol(instrument)
        tick: MT5NamedRecord | None = self._client.symbol_info_tick(instrument)
        if tick is None:
            raise self._failure("symbol_info_tick")
        values = tick._asdict()
        bid, ask = _number(values.get("bid"), "bid"), _number(values.get("ask"), "ask")
        if bid <= 0 or ask <= 0 or ask < bid:
            raise MT5Error("Invalid bid/ask quote")
        timestamp, milliseconds = _tick_timestamp(values)
        return MarketQuote(
            instrument=instrument,
            timestamp_utc=timestamp,
            timestamp_milliseconds=milliseconds,
            bid=bid,
            ask=ask,
            last=_optional_number(values, "last"),
            source="mt5",
            retrieved_at_utc=self._now(),
        )
