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

The injected MT5 adapter remains disabled by default and has only been exercised
with fakes. `connect()` uses three bounded initialization attempts with a
60-second MT5 initialization timeout and exponential backoff; historical rate
and tick calls retry raised client exceptions at most three times. Empty history
and malformed records are reported directly, not retried. MT5's history
functions do not expose a request-timeout argument, so an in-flight synchronous
history call cannot be safely interrupted by this adapter. Do not treat the
bounded retries as a hard end-to-end request deadline.

Use `run_pipeline_study` to bind a study request's UTC cutoff to
`retrieve_validated_bars`. Typed quality failures, empty history, and provider
errors are persisted as non-computing study results with status and stable
source/reason evidence. Invalid requests and unclassified exceptions still
raise. The helper is an offline integration seam; it does not initialize MT5.

SQLite migrations through schema v7 are transactional. Schema v4 upgrades
quote/tick event identity, schema v5 adds append-only study events and result
observations, schema v6 indexes the calendar-context cache identity, and schema
v7 blocks `INSERT OR REPLACE` from replacing existing study-event identities or
observations. Event identity excludes retrieval time: exact repeated quote/tick
events are rejected, while distinct updates sharing a timestamp are retained in
insertion order. Persistence APIs are insert-only; triggers guard updates,
deletes, and replacements on study-event tables, but do not prevent arbitrary
direct edits to other database tables.

Future integrations require bounded retries, supported timeouts, health
reporting, idempotent jobs, graceful shutdown, and independently reviewed
read-only access.

The AST safety scan allows MT5 imports only in `adapters/mt5` and only from the
read-only API allowlist in `security/trading_safety.py`. Phase 0.1 prohibits web
routes and Telegram handlers throughout the package. This static scan cannot
prove safety against all obfuscated or dynamically assembled behavior, so new
integration code and dependencies require human review.
