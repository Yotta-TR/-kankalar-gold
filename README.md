# KANKALAR GOLD AI

Research-only quantitative analysis and decision-support scaffold for intraday
gold research. This Phase 0 project contains interfaces and safe test doubles;
it does not connect to a broker, fetch external data, or implement trading logic.

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

This application is for research and decision support only. No broker adapter,
order-execution module, automatic-trading route, credential, or real market-price
fixture is included. `safe_mode` must remain enabled; live trading and Telegram
are disabled by validated configuration. The AST safety policy restricts MT5
imports to `adapters/mt5` and its explicit read-only API allowlist, and prohibits
web routes and Telegram command handlers throughout the application.

Static analysis is a defense-in-depth check, not a proof against every
dynamically constructed or obfuscated execution path. Preserve the adapter
boundary, review new dependencies and source changes, and require human review
before adding any broker or messaging integration.

## Architecture

- `core`: typed market, study, proposal, risk, lifecycle, and persistence contracts.
- `adapters.mock`: in-memory test provider; no network access.
- `storage`: append-oriented SQLite persistence for Phase 0 records; no broker access.
- `observability`: UTC logging with credential redaction.
- `config`: validated defaults for research-only use.
- `docs`: data dictionary, study catalogue, model card, backtest protocol,
  operations guidance, and architecture decisions.

## Next phase

Phase 1 may add a separately reviewed, read-only MT5 market-data adapter and
data-quality checks. First validate host/package compatibility and preserve the
no-order-execution boundary; do not begin until Phase 0 is reviewed.
