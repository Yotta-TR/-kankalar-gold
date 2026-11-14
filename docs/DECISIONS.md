# Architecture Decisions

## ADR-001: Research-only boundary in Phase 0

- **Decision:** Configuration validation fixes safe mode on, live trading off,
  Telegram off, and risk mode to virtual.
- **Reason:** The product specification requires manual decisions and forbids
  order placement. A test scans application source for execution calls/routes.
- **Consequence:** These controls cannot be enabled through Phase 0 YAML. A future
  change requires separate design and safety review.

## ADR-002: Typed contracts before implementations

- **Decision:** Define dataclass/Protocol boundaries for bars, study results,
  proposals, risk decisions, lifecycle events, providers, and persistence.
- **Reason:** Later phases can add independently tested adapters without coupling
  research contracts to broker, storage, or notification vendors.
- **Consequence:** Only an in-memory mock provider is implemented now.

## ADR-003: UTC timestamps at boundaries

- **Decision:** Reject naive datetimes and normalize aware inputs to UTC.
- **Reason:** Timestamp ambiguity can create lookahead and session errors.
- **Consequence:** Future adapters must explicitly interpret source timezones.

## ADR-004: Local SQLite persistence contract

- **Decision:** Provide an append-oriented SQLite implementation for bars, study
  results, and signal lifecycle events, using a caller-supplied local path.
- **Reason:** SQLite is the master specification's initial single-host storage
  choice and requires no broker integration or external service.
- **Consequence:** Schema initialization is explicit; duplicate primary records
  raise SQLite integrity errors instead of silently overwriting audit history.

## ADR-005: AST safety policy and constrained integration boundaries

- **Decision:** Scan all application Python files with AST rules. Permit MT5
  imports only below `adapters/mt5`, and permit only the explicit read-only API
  allowlist. Prohibit web route registration and Telegram command-handler
  registration throughout the application.
- **Reason:** Name-based text scanning was too easy to bypass through import
  aliases, dynamic access, or renamed handlers.
- **Consequence:** Static analysis is defense in depth, not a proof against
  arbitrary dynamic code, obfuscation, native extensions, or behavior hidden in
  dependencies. New integrations require human review and must preserve these
  module boundaries.

## ADR-006: Structured credential redaction

- **Decision:** Redact sensitive field names recursively in mappings/lists,
  formatted messages, JSON text, structured `LogRecord` extras, exception
  traces, and common Bearer values.
- **Reason:** Secret-bearing records may be logged as structured values rather
  than simple `key=value` text.
- **Consequence:** Redaction covers common secret field names and formats; it
  cannot recognize arbitrary secret values logged under innocuous names.

## ADR-007: StudyRunner, result cache and schema v3 (Phase 1B.3)

- **Decision:** `StudyRunner` runs one registered study (explicit ID and version)
  on one `ValidatedBarSet` for one timeframe, with a required UTC `as_of_utc`
  cutoff (every bar must have ended by it; it may not exceed `validated_at_utc`).
  Configuration is normalized by `RegisteredStudy` on both the normal and the
  data-failure path. The snapshot ID is computed before the warm-up check, so
  `INSUFFICIENT_DATA` results are cacheable. The cache key is study ID, study
  version, instrument, timeframe, `as_of_utc`, canonical normalized config,
  data snapshot ID and `CACHE_FORMAT_VERSION`. Ordinary compute exceptions and
  invalid outputs become persisted `ERROR` results with fixed codes
  (`STUDY_EXECUTION_ERROR`, `INVALID_STUDY_OUTPUT`); `BaseException` propagates.
  `ERROR` and `STALE` results and results without a snapshot are persisted but
  never served from the cache.
- **Validation:** `core/study_results.py` (depends only on `core.contracts`)
  holds the status matrix and provenance validation shared by the runner and
  SQLite deserialization, so `storage` never imports `studies`.
- **Schema v3:** additive `provenance_json` and `cache_format_version` columns
  plus a cache index, migrated atomically. Pre-v3 rows have NULL values and are
  never cache hits. A corrupt eligible row raises instead of being skipped.
- **Study version:** `RegisteredStudy.version` identifies the study's behavior;
  any output-affecting change requires a bump. It is not a Git SHA or the
  application version; application revision tracking is deferred.
- **Logging:** failures log a fixed message with study ID, version and a
  sanitized exception class name only; no message, traceback or `exc_info`.
- **Consequences:** `DataFailure` instrument/timeframe/cutoff binding is
  caller-asserted because `DataQualityReport` has no identity fields. Append-only
  storage has no unique constraint, so concurrent identical cache misses may
  create duplicate rows; this is accepted and readers use the newest row.

## ADR-008: F4 no-lookahead test harness and recorded pipeline boundary

- **Decision:** F4 includes a test-only harness and the production cutoff binding
  described below. No schema or cache-format change.
- **Method:** a result is a function of study version, configuration, cutoff and the
  selected snapshot. For the same cutoff and snapshot the full `StudyResult` must be
  exactly equal across repeats, fresh instances, evaluation orders and future-only
  mutations (append, or altering OHLCV/volume of bars ending after the cutoff).
  Moving the cutoff inside one bar may change only `as_of_utc` and cache identity;
  moving it past a newly completed bar legitimately changes snapshot and output.
  Every run uses a store that never hits the cache so a cached result cannot mask a
  leak. Comparison is exact; a numeric tolerance is deferred until a real study needs it.
- **Controls:** a causal canary (mean of the last three closes) plus deliberately
  faulty studies: a future-peeker (the only fixture given the full universe), a
  stateful repainter, and scripted clock/random consumers. A constant study only proves
  the harness's non-vacuity guards; constant output is not rejected for real studies.
  Sensitivity is checked only against a mutation the test author declares sensitive.
- **Guarantees and limits:** the harness detects side-channel, stateful and
  order-dependent leaks and selection errors visible in the full `StudyResult`. It
  cannot see leaks through channels it does not perturb (files, sockets, environment,
  unscripted clock/RNG, a held provider), which remain for the AST safety scan and review.
- **Reference selector:** `select_completed_bars` is a test oracle, not a production
  safeguard.
- **Boundary gap resolved (tests B1-B6):** pipeline `end_utc` is now the inclusive
  completion cutoff. It requests inclusive provider open-time bounds from
  `end_utc - history_days` through `end_utc - timeframe_duration`, and rejects
  provider overreach rather than trimming it. A bar ending at the cutoff is accepted.
  `ValidatedBarSet` records the UTC cutoff and rejects later-ending bars;
  `StudyRunner` requires its request `as_of_utc` to equal that cutoff before cache
  lookup or computation. B4 now guards against the previously characterized mismatch.
  Direct carrier construction asserts a cutoff but does not prove provider provenance.
  `retrieve_bars` applies the same range checks but its raw tuple carries no cutoff
  provenance; use `retrieve_validated_bars` for the normal study workflow.
