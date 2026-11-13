# KANKALAR GOLD AI

Research-only quantitative analysis and decision-support scaffold for intraday
gold research. No trading logic or order execution is implemented.

Phase 1A completes market-data contracts, offline quality checks, injectable
read-only MT5 adapter, configuration-based provider selection, historical
coverage validation, and an append-only versioned SQLite persistence layer.
Comprehensive testing with deterministic synthetic data verifies all components.
This is research-only software; do not use for live market analysis.

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
  validation and normalization, data-quality integration.
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

Phase 1B.3 adds a single-timeframe `StudyRunner` with an append-only, snapshot-keyed result cache (SQLite schema v3); no actual studies, strategies or Runner scheduling exist yet.
The adapter has only been tested with fakes, not against a real terminal. The
official MT5 `initialize()` may launch the terminal and use its last saved
account; do not call it until a separate read-only environment and terminal
setup are reviewed.
