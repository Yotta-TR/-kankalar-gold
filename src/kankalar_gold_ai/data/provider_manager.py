"""Provider manager: configuration-based selection, health reporting, no silent switching."""

from dataclasses import dataclass
from datetime import datetime, timezone

from kankalar_gold_ai.adapters.mock import MockMarketDataProvider
from kankalar_gold_ai.core.contracts import (
    DataStatus,
    MarketBar,
    MarketDataHealth,
    MarketDataProvider,
    as_utc,
)


@dataclass(frozen=True)
class ProviderConfig:
    """Explicit provider selection with no defaults or implicit switching."""

    source: str
    mt5_symbol: str | None = None

    def __post_init__(self) -> None:
        if self.source not in ("mock", "mt5"):
            raise ValueError(f"Unknown provider source: {self.source}")
        if self.source == "mt5":
            if not self.mt5_symbol or not self.mt5_symbol.strip():
                raise ValueError(
                    "MT5 provider requires mt5_symbol to be explicitly configured and non-blank"
                )
        if self.source == "mock" and self.mt5_symbol:
            raise ValueError("Mock provider does not accept mt5_symbol")


class ProviderManager:
    """Select, validate, and monitor a single market data provider."""

    def __init__(
        self,
        config: ProviderConfig,
        mock_bars: tuple[MarketBar, ...] | None = None,
    ) -> None:
        self._config = config
        self._provider: MarketDataProvider | None = None
        self._health_status: MarketDataHealth | None = None
        self._health_error: str | None = None

        if self._config.source == "mock":
            if mock_bars is None:
                mock_bars = ()
            self._provider = MockMarketDataProvider(mock_bars)
        elif self._config.source == "mt5":
            from kankalar_gold_ai.adapters.mt5.client import load_windows_client
            from kankalar_gold_ai.adapters.mt5.provider import MT5MarketDataProvider

            try:
                client = load_windows_client()
            except RuntimeError as e:
                self._health_error = str(e)
                self._health_status = MarketDataHealth(
                    status=DataStatus.UNAVAILABLE,
                    source="mt5",
                    checked_at_utc=as_utc(datetime.now(timezone.utc)),
                    detail=self._health_error,
                )
                return

            self._provider = MT5MarketDataProvider(
                client,
                selected_symbol=self._config.mt5_symbol,
            )

    def source(self) -> str:
        """Return the active provider source name."""
        return self._config.source

    def provider(self) -> MarketDataProvider:
        """Return the active provider; raise if initialization failed."""
        if self._provider is None:
            raise RuntimeError(
                f"Provider {self._config.source} is not available: {self._health_error}"
            )
        return self._provider

    def health(self) -> MarketDataHealth:
        """Report provider health; for MT5, connect() must have been called first."""
        if self._health_status is not None:
            return self._health_status

        if self._config.source == "mock":
            return MarketDataHealth(
                status=DataStatus.VALID,
                source="mock",
                checked_at_utc=as_utc(datetime.now(timezone.utc)),
                detail="Mock provider is in-memory and always available",
            )

        from kankalar_gold_ai.adapters.mt5.provider import MT5MarketDataProvider

        if isinstance(self._provider, MT5MarketDataProvider):
            return self._provider.health()

        raise RuntimeError("Provider type is unknown")

    def __repr__(self) -> str:
        return f"ProviderManager(source={self._config.source!r})"
