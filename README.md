# KANKALAR GOLD AI

Research-only quantitative analysis and decision-support scaffold for intraday
gold research. No trading logic or order execution is implemented.

Phase 1A completes market-data contracts, offline quality checks, injectable
read-only MT5 adapter, configuration-based provider selection, historical
coverage validation, and an append-oriented versioned SQLite persistence layer
(schema v10).
Comprehensive testing with deterministic synthetic data verifies all components.
This is research-only software; do not use for live market analysis.

The data layer includes an explicit broker/instrument session-calendar contract,
calendar-aware freshness, quote/tick integrity checks, and a fail-closed bridge
from historical retrieval to single-study execution. No broker schedule,
timezone, holiday calendar, or freshness limit is configured by default;
calendar tests use synthetic schedules and do not establish actual
broker-session behavior.

The optional `mt5` dependency is restricted to Windows AMD64. As checked on
12 November 2026, the official PyPI release provides Windows x86-64 wheels
for CPython 3.10–3.14; verify compatibility again before deployment. The
adapter loads the optional package only when explicitly requested on Windows;
tests inject a fake client and do not load or connect to a terminal.

## Requirements and setup

- Python 3.11 or newer
- Install the project and development tools in an isolated environment:

  ```bash
  python -m pip install -e ".[dev]"
  ```

- Copy and review `config/defaults.yaml` for the validated research configuration.
  Phase 0 intentionally disallows live trading, Telegram delivery, and non-virtual
  risk settings.

## Development checks

```bash
pytest
ruff check .
ruff format --check .
mypy src
git diff --check
```

The tests use only deterministic, clearly labeled synthetic fixtures and never
make live network calls.

## Safety boundary

This application is for research and decision support only. No order-execution
module, automatic-trading route, credential, or real market-price fixture is
included. `safe_mode` must remain enabled; live trading and Telegram are disabled
by validated configuration. The AST safety policy restricts MT5
imports to `adapters/mt5` and its explicit read-only API allowlist, and prohibits
web routes and Telegram command handlers throughout the application.

Static analysis is a defense-in-depth check, not a proof against every
dynamically constructed or obfuscated execution path. Preserve the adapter
boundary, review new dependencies and source changes, and require human review
before adding any broker or messaging integration.

## Architecture

- `core`: typed market, study, proposal, risk, lifecycle, and persistence contracts.
- `data`: configuration-based provider manager, historical data pipeline with
  validation and normalization, data-quality integration, and explicit
  versioned session-calendar contracts.
- `adapters.mock`: in-memory test provider; no network access.
- `adapters.mt5`: injected read-only terminal adapter; Windows only, disabled
  by default.
- `storage`: append-oriented SQLite persistence with versioned migrations. Schema
  changes and version updates run in one explicit transaction; a failed migration
  rolls back the full upgrade and preserves Phase 0 data.
- `observability`: UTC logging with credential redaction.
- `config`: validated defaults for research-only use.
- `docs`: data dictionary, study catalogue, model card, backtest protocol,
  operations guidance, and architecture decisions.

## Next phase

Phase 1B.3 (single-timeframe `StudyRunner`, snapshot- and calendar-context-keyed
result cache) and the F4 no-lookahead harness are implemented. The pipeline's
inclusive bar-completion `end_utc` is bound to the runner's `as_of_utc`, and
provider overreach is rejected (ADR-008). Use `retrieve_validated_bars` for
studies; `retrieve_bars` returns a raw tuple without cutoff provenance.
`run_pipeline_study` connects retrieval and study execution, mapping typed data
and provider failures to persisted non-computing study results. The
`volatility.atr`, `trend.ma_slope_order`, `trend.adx_dm`, `trend.rsi`, and
`trend.roc_momentum`, `trend.macd`, `trend.donchian`, `structure.swings`,
`structure.bos_choch`, `structure.sr_zones`, and `structure.liquidity_sweep`
studies and independent multi-timeframe coordination are
descriptive research tools, not validated trading strategies. RSI describes
its own movement, not a market regime; each ROC run uses one explicit horizon.
MACD stores its histogram as the scalar result and its other components as
evidence. Donchian uses prior completed bars and reports unclipped channel
position. MACD and Donchian require explicit periods and are descriptive only.
Confirmed swings return typed, immutable events: pivot time is distinct from
confirmation time, and D1 completion uses the validated calendar timezone.
Calendar identity participates in cache lookup independently of the unchanged
bar-data snapshot. Without an explicit calendar, D1 completion uses fixed
24-hour UTC days. SQLite schema v5 stores events append-only with their result observations;
schema v6 adds the calendar-context cache identity, and schema v7 blocks
replacement using existing event and observation identities.
`structure.bos_choch` v0.1.0 uses only swings confirmed by the break candle's
open, and requires a completed close strictly across the level from a previous
close on the unbroken side. It selects the nearest crossed level, breaking
equal-price ties by latest confirmation; the first break is `INITIAL_BREAK`,
same-direction breaks are `BOS`, and opposite breaks are `CHOCH` with a bias
flip. All levels crossed in that candle are consumed, even when only the nearest
is reported. Schema v8 persists immutable break records and observations and
strengthens replacement guards against explicit rowid collisions. The initial
bias depends on the beginning of the supplied history; keep the history window
consistent for replay. These events are descriptive and have no predictive or
trading approval.
`structure.sr_zones` v0.1.0 uses explicit swing windows, pivot lookback and
price tolerance to group confirmed lows into support and highs into resistance.
Each zone requires at least two swings; its full price range must fit the
tolerance measured from its lowest member. Schema v9 stores immutable zone
records and observations. A membership change produces a new identity.
Volume-based strength is unavailable and unimplemented; zones are descriptive.
`structure.liquidity_sweep` v0.1.0 describes completed wick penetrations and
reclaims of support or resistance zones known at the candle open. All swing,
zone and minimum-overshoot parameters are explicit. Both sides can be recorded
independently, without inferring their intrabar order. Schema v10 persists
immutable sweep events and their referenced zones; these observations are not
predictive, directional trade recommendations or signals.
Strategies and runner scheduling remain unimplemented.

The MT5 adapter has bounded connection/history retries and a timeout for
initialization, but MT5's synchronous history functions do not expose a
cancellable request timeout. The adapter has only been tested with fakes, not
against a real terminal. The official MT5 `initialize()` may launch the
terminal and use its last saved account; do not call it until a separate
read-only environment and terminal setup are reviewed.
