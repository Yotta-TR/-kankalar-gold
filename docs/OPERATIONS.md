# Operations

Phase 0 is a local Python package with no network, broker, or Telegram
connections. Its SQLite persistence accepts an explicit local database path and
does not initialize or connect to anything until `SQLitePersistence.initialize()`
is called. Install development dependencies in a virtual environment using
`python -m pip install -e ".[dev]"`.

Run `pytest`, `ruff check .`, `ruff format --check .`, `mypy src`, and
`git diff --check` before review. No credentials are needed. Do not place secrets
in logs or commit them; `.env` and `.env.*` are ignored, with only the
placeholder-only `.env.example` allowed. Runtime data belongs under ignored
`data/`, never in source control.

Future integrations require bounded retries, timeouts, health reporting,
idempotent jobs, graceful shutdown, and independently reviewed read-only access.

The AST safety scan allows MT5 imports only in `adapters/mt5` and only from the
read-only API allowlist in `security/trading_safety.py`. Phase 0.1 prohibits web
routes and Telegram handlers throughout the package. This static scan cannot
prove safety against all obfuscated or dynamically assembled behavior, so new
integration code and dependencies require human review.
