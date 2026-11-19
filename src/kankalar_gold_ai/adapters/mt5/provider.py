"""Injected, read-only MT5 data access; no credentials or trading APIs."""

import math
import time
from collections.abc import Iterable
from collections.abc import Mapping as MappingABC
from datetime import datetime, timedelta, timezone
from numbers import Integral, Real
from typing import Callable, Mapping, TypeVar, cast

from kankalar_gold_ai.adapters.mt5.client import (
    MT5ArrayRecord,
    MT5Client,
    MT5NamedRecord,
)
from kankalar_gold_ai.core.contracts import (
    DataStatus,
    InstrumentSpecification,
    MarketBar,
    MarketDataHealth,
    MarketQuote,
    MarketTick,
    as_utc,
)
from kankalar_gold_ai.data.errors import DataProviderError, EmptyHistoricalDataError
from kankalar_gold_ai.data.quality import TIMEFRAME_DURATIONS, validate_bar_records

_TICK_FLAG_BID = 2
_TICK_FLAG_ASK = 4
_TICK_FLAG_LAST = 8
_TICK_FLAG_VOLUME = 16
_TICK_FLAG_UINT_MAX = (1 << 32) - 1
_TICK_VOLUME_UINT_MAX = (1 << 64) - 1
_INITIALIZE_TIMEOUT_MS = 60_000
_MAX_CONNECT_ATTEMPTS = 3
_MAX_HISTORY_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 0.25


class MT5Error(DataProviderError):
    """An explicit read-only terminal or data error."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            source_id="mt5",
            reason_code="MT5_PROVIDER_ERROR",
        )


class MT5EmptyHistoryError(MT5Error, EmptyHistoricalDataError):
    """The terminal returned an empty history, rather than a terminal failure."""


_Result = TypeVar("_Result")


def _client_call(operation: str, call: Callable[[], _Result]) -> _Result:
    try:
        return call()
    except Exception as error:
        raise MT5Error(f"{operation} failed: client raised {type(error).__name__}") from error


def _response_records(response: object, operation: str, kind: str) -> tuple[object, ...]:
    if isinstance(response, (str, bytes, MappingABC)):
        raise MT5Error(f"{operation} returned an invalid {kind} collection")
    try:
        if not isinstance(response, Iterable):
            raise TypeError("response is not iterable")
        return tuple(response)
    except Exception as error:
        raise MT5Error(f"{operation} returned an invalid {kind} collection") from error


def _record_mapping(record: object, operation: str) -> Mapping[str, object]:
    try:
        values = cast(MT5NamedRecord, record)._asdict()
    except AttributeError as error:
        raise MT5Error(f"{operation} returned a record without usable _asdict()") from error
    except TypeError as error:
        raise MT5Error(f"{operation} returned a record without usable _asdict()") from error
    except Exception as error:
        raise MT5Error(f"{operation} record _asdict() failed") from error
    if not isinstance(values, MappingABC):
        raise MT5Error(f"{operation} record _asdict() returned a non-mapping")
    try:
        return dict(values)
    except Exception as error:
        raise MT5Error(f"{operation} record mapping could not be read") from error


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise MT5Error(f"Missing or invalid numeric field: {field}")
    try:
        result = float(value)
    except Exception as error:
        raise MT5Error(f"Could not convert numeric field: {field}") from error
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
    seconds_value = values.get("time")
    seconds = _number(seconds_value, "time")
    if seconds < 0 or not seconds.is_integer():
        raise MT5Error("Invalid MT5 timestamp: time must be whole non-negative seconds")
    timestamp = _timestamp(seconds)
    milliseconds_value = values.get("time_msc")
    if milliseconds_value is None:
        milliseconds = None
    elif isinstance(milliseconds_value, bool) or not isinstance(milliseconds_value, Integral):
        raise MT5Error("Invalid MT5 timestamp: time_msc must be a non-negative integer")
    else:
        milliseconds = int(milliseconds_value)
        if milliseconds < 0 or milliseconds >= 1 << 63:
            raise MT5Error("Invalid MT5 timestamp: time_msc must be a non-negative integer")
    if milliseconds is None:
        return timestamp, None
    second = math.floor(timestamp.timestamp())
    if not second * 1000 <= milliseconds < (second + 1) * 1000:
        raise MT5Error("Tick seconds and milliseconds are inconsistent")
    return _timestamp(milliseconds / 1000), milliseconds


def _tick_flags(values: Mapping[str, object]) -> int:
    flags = values.get("flags")
    if isinstance(flags, bool) or not isinstance(flags, Integral):
        raise MT5Error("Invalid tick flags: expected an unsigned 32-bit integer")
    flags_value = int(flags)
    if flags_value < 0 or flags_value > _TICK_FLAG_UINT_MAX:
        raise MT5Error("Invalid tick flags: expected an unsigned 32-bit integer")
    return flags_value


def _validate_tick_values(
    values: Mapping[str, object],
    previous_values: Mapping[str, object] | None,
) -> tuple[datetime, int | None, int, bool, float | None]:
    timestamp, milliseconds = _tick_timestamp(values)
    flags = _tick_flags(values)
    bid = _number(values.get("bid"), "bid")
    ask = _number(values.get("ask"), "ask")
    last = _optional_number(values, "last")
    if bid < 0 or ask < 0:
        raise MT5Error("Invalid bid/ask tick: prices must not be negative")
    if last is not None and last < 0:
        raise MT5Error("Invalid tick last: price must not be negative")
    volume = values.get("volume")
    volume_real = _optional_number(values, "volume_real")
    volume_value: int | None = None
    if volume is not None:
        if isinstance(volume, bool) or not isinstance(volume, Integral):
            raise MT5Error("Invalid tick volume: expected an integer")
        volume_value = int(volume)
        if volume_value < 0 or volume_value > _TICK_VOLUME_UINT_MAX:
            raise MT5Error("Invalid tick volume: expected a non-negative unsigned integer")
    if volume_real is not None and volume_real < 0:
        raise MT5Error("Invalid tick volume_real: must not be negative")

    bid_changed = bool(flags & _TICK_FLAG_BID)
    ask_changed = bool(flags & _TICK_FLAG_ASK)
    if flags & _TICK_FLAG_LAST and last is None:
        raise MT5Error("Invalid tick last: LAST flag is set but last is missing")
    if flags & _TICK_FLAG_VOLUME and volume is None and volume_real is None:
        raise MT5Error("Invalid tick volume: VOLUME flag is set but volume is missing")
    empty_book_event = bid == 0 and ask == 0 and flags == (_TICK_FLAG_BID | _TICK_FLAG_ASK)
    previous_bid = None if previous_values is None else previous_values.get("bid")
    previous_ask = None if previous_values is None else previous_values.get("ask")
    bid_zero_carried = bid == 0 and not bid_changed and previous_bid == 0
    ask_zero_carried = ask == 0 and not ask_changed and previous_ask == 0
    if bid == 0 and not (empty_book_event or bid_zero_carried):
        raise MT5Error("Invalid bid value: zero requires a flagged empty book or carried value")
    if ask == 0 and not (empty_book_event or ask_zero_carried):
        raise MT5Error("Invalid ask value: zero requires a flagged empty book or carried value")
    empty_book = empty_book_event
    if bid > 0 and ask > 0 and ask < bid:
        raise MT5Error("Invalid bid/ask tick: ask is below bid")

    if previous_values is not None:
        for field, changed in (
            ("bid", bid_changed),
            ("ask", ask_changed),
            ("last", bool(flags & _TICK_FLAG_LAST)),
            ("volume", bool(flags & _TICK_FLAG_VOLUME)),
            ("volume_real", bool(flags & _TICK_FLAG_VOLUME)),
        ):
            value = values.get(field)
            previous = previous_values.get(field)
            if not changed and value is not None and previous is not None and value != previous:
                raise MT5Error(f"Tick field {field} changed without its change flag")

    tick_volume = (
        volume_real
        if volume_real is not None
        else None
        if volume_value is None
        else float(volume_value)
    )
    return timestamp, milliseconds, flags, empty_book, tick_volume


def _row_values(row: object, fields: tuple[str, ...]) -> dict[str, object]:
    values: dict[str, object] = {}
    for field in fields:
        try:
            values[field] = cast(MT5ArrayRecord, row)[field]
        except (KeyError, IndexError):
            continue
        except Exception as error:
            raise MT5Error(f"could not read response field {field}") from error
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
        self._cleanup_required = False
        self._connected = False

    def _now(self) -> datetime:
        return as_utc(self._clock())

    def _failure(self, operation: str) -> MT5Error:
        try:
            response = _client_call("last_error", self._client.last_error)
        except MT5Error as error:
            raise MT5Error(f"{operation} failed; MT5 last_error() also failed") from error
        if (
            not isinstance(response, tuple)
            or len(response) != 2
            or type(response[0]) is not int
            or not isinstance(response[1], str)
        ):
            raise MT5Error(f"{operation} failed; MT5 last_error() returned a malformed response")
        code, _message = response
        return MT5Error(f"{operation} failed (MT5 {code})")

    def _require_connected(self) -> None:
        if not self._connected:
            raise MT5Error("MT5 terminal is not initialized")

    def _require_symbol(self, instrument: str) -> None:
        if self._selected_symbol is None or instrument != self._selected_symbol:
            raise MT5Error("Explicitly select the requested broker symbol first")

    def connect(self) -> None:
        if self._connected:
            return
        self._connected = False
        if self._cleanup_required:
            _client_call("shutdown", self._client.shutdown)
            self._cleanup_required = False

        for attempt in range(_MAX_CONNECT_ATTEMPTS):
            self._cleanup_required = True
            try:
                initialized = _client_call(
                    "initialize",
                    lambda: self._client.initialize(timeout=_INITIALIZE_TIMEOUT_MS),
                )
                if type(initialized) is not bool:
                    raise MT5Error("initialize returned a malformed status")
                if initialized:
                    self._connected = True
                    return
                error = self._failure("initialize")
            except MT5Error as failure:
                error = failure
            if attempt + 1 == _MAX_CONNECT_ATTEMPTS:
                raise error
            _client_call("shutdown", self._client.shutdown)
            self._cleanup_required = False
            time.sleep(_RETRY_DELAY_SECONDS * (2**attempt))

    @staticmethod
    def _history_call(operation: str, call: Callable[[], _Result]) -> _Result:
        for attempt in range(_MAX_HISTORY_ATTEMPTS):
            try:
                return _client_call(operation, call)
            except MT5Error:
                if attempt + 1 == _MAX_HISTORY_ATTEMPTS:
                    raise
                time.sleep(_RETRY_DELAY_SECONDS * (2**attempt))
        raise AssertionError("bounded history retry loop did not return or raise")

    def shutdown(self) -> None:
        self._connected = False
        if self._cleanup_required:
            _client_call("shutdown", self._client.shutdown)
            self._cleanup_required = False

    def health(self) -> MarketDataHealth:
        now = self._now()
        if not self._connected:
            return MarketDataHealth(DataStatus.UNAVAILABLE, "mt5", now, "Not initialized")
        try:
            info = _client_call("terminal_info", self._client.terminal_info)
            if info is None:
                raise self._failure("terminal_info")
            connected = _record_mapping(info, "terminal_info").get("connected")
            if not isinstance(connected, bool):
                raise MT5Error("terminal_info is missing connected status")

            version = _client_call("version", self._client.version)
            if version is None:
                raise self._failure("version")
            if (
                not isinstance(version, tuple)
                or len(version) != 3
                or type(version[0]) is not int
                or type(version[1]) is not int
                or not isinstance(version[2], str)
            ):
                raise MT5Error("version returned malformed terminal version")

            if not connected:
                self._connected = False
            return MarketDataHealth(
                DataStatus.VALID if connected else DataStatus.UNAVAILABLE,
                "mt5",
                now,
                "Connected" if connected else "Terminal disconnected",
                ".".join(str(item) for item in version),
            )
        except Exception:
            self._connected = False
            raise

    def discover_symbols(self) -> tuple[InstrumentSpecification, ...]:
        self._require_connected()
        symbols = _client_call("symbols_get", self._client.symbols_get)
        if symbols is None:
            raise self._failure("symbols_get")
        symbol_records = _response_records(symbols, "symbols_get", "symbol")
        candidates: list[InstrumentSpecification] = []
        for symbol in symbol_records:
            values = _record_mapping(symbol, "symbols_get")
            try:
                name = _optional_text(values, "name")
            except MT5Error as error:
                raise MT5Error(f"symbols_get returned an invalid symbol name: {error}") from error
            if name is None or not name.strip():
                raise MT5Error("symbols_get returned a symbol record with missing or blank name")
            if "XAUUSD" in name.upper() or "GOLD" in name.upper():
                candidates.append(self.symbol_info(name))
        return tuple(candidates)

    def symbol_info(self, symbol: str) -> InstrumentSpecification:
        self._require_connected()
        if not symbol.strip():
            raise ValueError("symbol must not be blank")
        info = _client_call("symbol_info", lambda: self._client.symbol_info(symbol))
        if info is None:
            raise self._failure("symbol_info")
        values = _record_mapping(info, "symbol_info")
        try:
            if _optional_text(values, "name") != symbol:
                raise MT5Error("returned a different or missing symbol name")
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
        except MT5Error as error:
            raise MT5Error(f"symbol_info returned invalid data: {error}") from error

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
        rows = self._history_call(
            "copy_rates_range",
            lambda: self._client.copy_rates_range(instrument, frame, start, end),
        )
        if rows is None:
            raise self._failure("copy_rates_range")
        bar_records = _response_records(rows, "copy_rates_range", "bar")
        now = self._now()
        bars: list[MarketBar] = []
        fields = ("time", "open", "high", "low", "close", "tick_volume", "real_volume", "spread")
        for index, row in enumerate(bar_records):
            try:
                values = _row_values(row, fields)
                timestamp = _timestamp(values.get("time"))
                if not start <= timestamp <= end:
                    raise MT5Error("bar timestamp is outside the requested range")
                try:
                    bar = MarketBar(
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
                except ValueError as error:
                    raise MT5Error(f"Invalid OHLC data: {error}") from error
                bars.append(bar)
            except MT5Error as error:
                raise MT5Error(
                    f"copy_rates_range returned malformed bar at index {index}: {error}"
                ) from error
            except Exception as error:
                raise MT5Error(
                    "copy_rates_range returned malformed bar "
                    f"at index {index} ({type(error).__name__})"
                ) from error
        result = tuple(bars)
        if not result:
            raise MT5EmptyHistoryError("Bar quality INSUFFICIENT_DATA: EMPTY_HISTORY")
        report = validate_bar_records(
            result,
            timeframe,
            now,
            check_completion=timeframe != "D1",
        )
        if report.status != DataStatus.VALID:
            raise MT5Error(f"Bar quality {report.status}: {', '.join(report.reasons)}")
        if timeframe != "D1" and result[-1].timestamp_utc + TIMEFRAME_DURATIONS[timeframe] > now:
            raise MT5Error("Incomplete bar")
        return result

    def get_ticks(
        self, instrument: str, start_utc: datetime, end_utc: datetime
    ) -> tuple[MarketTick, ...]:
        """Return full-state ticks in time order.

        A tick with bid=ask=0 and exactly the BID/ASK flags represents the
        documented empty order book; consumers must not treat it as an executable quote.
        """
        start, end = self._range(instrument, start_utc, end_utc)
        rows = self._history_call(
            "copy_ticks_range",
            lambda: self._client.copy_ticks_range(
                instrument, start, end, self._client.COPY_TICKS_ALL
            ),
        )
        if rows is None:
            raise self._failure("copy_ticks_range")
        tick_records = _response_records(rows, "copy_ticks_range", "tick")
        if not tick_records:
            raise MT5EmptyHistoryError("No ticks returned; historical coverage is unavailable")
        now = self._now()
        fields = (
            "time",
            "time_msc",
            "bid",
            "ask",
            "last",
            "volume",
            "volume_real",
            "flags",
        )
        ticks = []
        seen_ticks: set[MarketTick] = set()
        previous_values: Mapping[str, object] | None = None
        previous_timestamp: datetime | None = None
        for index, row in enumerate(tick_records):
            try:
                values = _row_values(row, fields)
                timestamp, milliseconds, flags, _empty_book, tick_volume = _validate_tick_values(
                    values, previous_values
                )
                if not start <= timestamp <= end:
                    raise MT5Error("tick timestamp is outside the requested range")
                if previous_timestamp is not None and timestamp < previous_timestamp:
                    raise MT5Error("ticks are not in chronological order")
                tick = MarketTick(
                    instrument=instrument,
                    timestamp_utc=timestamp,
                    timestamp_milliseconds=milliseconds,
                    bid=_optional_number(values, "bid"),
                    ask=_optional_number(values, "ask"),
                    last=_optional_number(values, "last"),
                    volume=tick_volume,
                    flags=flags,
                    source="mt5",
                    retrieved_at_utc=now,
                )
                if tick in seen_ticks:
                    raise MT5Error("duplicate tick record")
                seen_ticks.add(tick)
                ticks.append(tick)
                previous_values = dict(values)
                previous_timestamp = timestamp
            except MT5Error as error:
                raise MT5Error(
                    f"copy_ticks_range returned malformed tick at index {index}: {error}"
                ) from error
            except Exception as error:
                raise MT5Error(
                    "copy_ticks_range returned malformed tick "
                    f"at index {index} ({type(error).__name__})"
                ) from error
        return tuple(ticks)

    def latest_quote(self, instrument: str) -> MarketQuote:
        self._require_connected()
        self._require_symbol(instrument)
        tick: MT5NamedRecord | None = _client_call(
            "symbol_info_tick", lambda: self._client.symbol_info_tick(instrument)
        )
        if tick is None:
            raise self._failure("symbol_info_tick")
        values = _record_mapping(tick, "symbol_info_tick")
        try:
            timestamp, milliseconds, _flags, empty_book, _volume = _validate_tick_values(
                values, None
            )
            bid, ask = _number(values.get("bid"), "bid"), _number(values.get("ask"), "ask")
            if empty_book or bid <= 0 or ask <= 0 or ask < bid:
                raise MT5Error("Invalid bid/ask quote")
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
        except MT5Error as error:
            raise MT5Error(f"symbol_info_tick returned malformed quote: {error}") from error
        except Exception as error:
            raise MT5Error(
                f"symbol_info_tick returned malformed quote ({type(error).__name__})"
            ) from error
