"""Narrow, injectable interface to the official read-only MT5 functions."""

import platform
from datetime import datetime
from typing import Protocol, Sequence, cast


class MT5NamedRecord(Protocol):
    def _asdict(self) -> dict[str, object]: ...


class MT5ArrayRecord(Protocol):
    def __getitem__(self, key: str) -> object: ...


class MT5Client(Protocol):
    TIMEFRAME_M1: int
    TIMEFRAME_M5: int
    TIMEFRAME_M15: int
    TIMEFRAME_H1: int
    TIMEFRAME_H4: int
    TIMEFRAME_D1: int
    COPY_TICKS_ALL: int

    def initialize(self) -> bool: ...

    def shutdown(self) -> None: ...

    def version(self) -> tuple[int, int, str] | None: ...

    def terminal_info(self) -> MT5NamedRecord | None: ...

    def last_error(self) -> tuple[int, str]: ...

    def symbols_get(self) -> Sequence[MT5NamedRecord] | None: ...

    def symbol_info(self, symbol: str) -> MT5NamedRecord | None: ...

    def symbol_info_tick(self, symbol: str) -> MT5NamedRecord | None: ...

    def copy_rates_range(
        self, symbol: str, timeframe: int, start: datetime, end: datetime
    ) -> Sequence[MT5ArrayRecord] | None: ...

    def copy_ticks_range(
        self, symbol: str, start: datetime, end: datetime, flags: int
    ) -> Sequence[MT5ArrayRecord] | None: ...


def load_windows_client() -> MT5Client:
    """Load MT5 only on request, and only on Windows; never connect here."""
    if platform.system() != "Windows":
        raise RuntimeError("The official MetaTrader5 client requires Windows")
    try:
        import MetaTrader5  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("Install the optional MetaTrader5 dependency on Windows") from error
    return cast(MT5Client, MetaTrader5)
