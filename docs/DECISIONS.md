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

## ADR-009: Independent study execution across supported timeframes

- **Decision:** One registered `(study_id, version)` may declare multiple
  `required_timeframes`; that set means supported, independent single-timeframe
  invocations. `StudyRunner` still accepts exactly one `StudyRunRequest` and one
  matching `ValidatedBarSet`, and returns exactly one `StudyResult`. It accepts
  a request only when its timeframe is a member of the registered set.
- **Reason:** Ordinary studies such as ATR can be executed independently on
  several timeframes without duplicate registry identities or forcing
  multi-timeframe inputs into their calculation contracts.
- **Identity:** Every timeframe execution retains its own `StudyResult.timeframe`,
  input snapshot and cache identity. The existing cache key already includes
  timeframe; the SQLite schema and `StudyResult` contract do not change.
- **Compatibility:** Singleton timeframe registrations retain their existing
  behavior. Unsupported timeframes fail during study resolution before cache
  lookup, computation or persistence.
- **Scope:** `volatility.atr` v0.1.0 supports M1, M5, M15, H1, H4 and D1 as
  independent runs. Coordinating results at a common cutoff, freshness rules,
  composite snapshots and persisted analysis bundles are covered by later
  decisions; they are not implemented by this decision.

## ADR-010: Multi-timeframe request contracts and full preflight

- **Decision:** Preserve `StudyRunner` as a single-timeframe component. Add
  immutable execution/request, freshness-policy, preflight, and aggregate-result
  contracts in a separate multi-timeframe module. One request may select any
  non-empty subset of M1, M5, M15, H1, H4 and D1. Every requested timeframe
  maps to exactly one `ValidatedBarSet` or `DataFailure`; inputs are reused by
  all executions targeting that timeframe.
- **Preflight:** A public, non-persisting `StudyRunner.prepare` validates one
  request/input pair, normalizes configuration, and records the individual
  snapshot/window. Full preflight checks exact timeframe-key matching, common
  instrument/cutoff, source identity, supported registrations, normalized
  duplicate execution identities and freshness before any study execution.
  Preparation performs no cache lookup, computation or persistence.
- **Freshness:** The fixed policy `one-timeframe-duration` v1.0.0 records an
  explicit maximum age for each supported timeframe, exactly equal to that
  timeframe's duration. Freshness is inclusive at the boundary; there are no
  automatic weekend or market-closure exemptions. All validated bar sets must
  have the same source string.
- **Identity:** The canonical input manifest records the common instrument and
  UTC cutoff, coordinator and freshness-policy versions/configuration, source,
  normalized requested executions, component data snapshots/windows, bar ages,
  and pre-execution eligibility. It excludes final `StudyResult` statuses.
  SHA-256 input identity is separate from the result identity, which includes
  canonical final result records and statuses. Existing component snapshots,
  cache keys, StudyResult and SQLite schema are unchanged.
- **Canonicalization:** Manifest JSON is UTF-8, sorted-key, compact JSON with
  non-finite numbers rejected. Timestamps use canonical UTC ISO-8601 with `Z`;
  durations use integer microseconds. Components sort by supported timeframe
  order, study ID, version, then canonical normalized-config JSON. Input and
  result identities use distinct versioned SHA-256 prefixes.
- **Result contract:** Component results remain individual `StudyResult`s.
  Aggregate completeness is `COMPLETE`, `PARTIAL`, or
  `NO_AVAILABLE_COMPONENTS`; no score, probability, direction fusion, or
  trading recommendation is introduced.
- **Scope:** This stage implements contracts and complete preflight only. It
  does not execute component runs, persist aggregate results, change the
  SQLite schema, or add a cross-timeframe study algorithm.

## ADR-011: Multi-timeframe execution coordinator

- **Decision:** `MultiTimeframeAnalysisCoordinator` performs full preflight first,
  then executes each component in canonical order through the existing
  single-timeframe runner. Valid fresh inputs, including insufficient warm-up,
  use `run`; explicit data failures use `run_data_failure`. Stale valid bars
  become a `STALE` data failure checked at the common cutoff, with reason
  `FRESHNESS_MAX_AGE_EXCEEDED`, and are never computed. Their original
  snapshots and ages remain in the preflight manifest.
- **Persistence:** Individual runner cache and persistence behavior is unchanged.
  Computation errors become individual `ERROR` results; infrastructure and
  persistence exceptions propagate. Component persistence is not an all-or-nothing
  transaction: earlier results may already be stored when a later operation fails.
  Aggregate manifests and identities are returned, not persisted as a bundle.
- **Freshness:** `data.quality.TIMEFRAME_DURATIONS` supplies the existing fixed
  one-duration policy values. Policy version and explicit canonical effective
  configuration remain unchanged.
- **Scope:** No aggregation of directional conclusions, scores, probabilities,
  recommendations, or real order execution is introduced.

## ADR-012: Descriptive SMA ordering and fast slope

- **Decision:** Implement `trend.ma_slope_order` v0.1.0 as independent
  single-timeframe M1, M5, M15, H1, H4 and D1 runs. All three SMA periods,
  slope lookback and flat fractional-slope threshold are explicit configuration.
  EMA and any universal 20/50/200 default are not included.
- **Definition:** Calculate trailing close SMAs using `math.fsum`. The raw
  fast-SMA slope is `(fast_now - fast_previous) / lookback`; normalized slope
  divides that difference by the previous fast SMA and then by lookback.
  Warm-up is `max(slow_period, fast_period + lookback)`. Strict ordering and
  threshold comparisons produce the nine states specified in the catalogue.
- **Output and safety:** Only up/up and down/down are respectively bullish and
  bearish descriptive alignment; all other available states are neutral.
  Positive finite subnormal denominators are permitted; non-positive
  denominators and non-finite arithmetic fail through the runner's `ERROR`
  path. A neutral state does not assert a sideways market.
- **Scope:** This is not a forecast, trading recommendation or probability.
  No cross-timeframe combination, external data, or ATR dependency is added.

## ADR-013: Wilder DMI/ADX descriptive study

- **Decision:** Implement `trend.adx_dm` v0.1.0 as independent single-timeframe
  runs on M1, M5, M15, H1, H4 and D1. The master specification names ADX and
  directional movement but leaves the formula and period unresolved; this ADR
  records implementation choices rather than changing the canonical spec.
- **Configuration and indexing:** Require exactly one explicit
  `adx_period`, a built-in integer of at least 2, excluding booleans; use no
  default or maximum. Omit the first supplied bar's DM/TR observation, seed
  Wilder-smoothed sums from the next N observations, seed ADX from the first N
  DX values, and require `2 * adx_period` bars for the first output at index
  `2 * adx_period - 1`. Process every supplied observation; minimum warm-up is
  not a convergence guarantee.
- **Zero policy:** Exact zero smoothed TR with zero DM yields zero DIs and DX;
  positive TR with both DM sums zero also yields zero DIs and DX. Include zero
  DX in ADX initialization and recurrence. ADX decays when computed DX is zero;
  zero raw DM alone does not force zero DX, since residual one-sided smoothed DM
  can preserve dominance. Zero TR with non-zero DM is an arithmetic invariant
  error. No epsilon/tolerance is used. This differs from TA-Lib zero handling
  and is intentional project behavior.
- **Numerics and output:** Compute signed normalized DI dominance from scaled
  smoothed DM sums, not from already divided DIs, so subnormal DI underflow
  does not erase dominance. Reject non-finite arithmetic or values outside
  mathematical bounds through the runner's snapshot-bound `ERROR` path; do
  not clamp. `StudyOutput` remains unchanged: `value` is ADX,
  `normalized_value` is dominance or `None` when both DMs are zero, state and
  direction represent DI dominance, and evidence carries formatted latest
  +DI/-DI for human audit. Separate first-class DI fields require a future
  shared-contract decision.
- **Scope:** OHLC only; no volume, threshold, default period, cross-timeframe
  fusion, prediction, or trading recommendation is introduced. Existing
  runner, coordinator, cache, persistence, and SQLite schema remain unchanged.

## ADR-014: Explicit session calendars and closure-aware freshness

- **Decision:** Represent broker/instrument schedules as an immutable,
  versioned `SessionCalendar` scoped to an explicit provider and instrument,
  with source provenance and reference, IANA timezone, effective interval,
  separate UTC coverage interval, weekly local-time sessions, holidays, and
  exceptional UTC closures. Effective dates define when a schedule applies;
  coverage dates define the portion for which the schedule is known and may
  classify intervals. Coverage must lie within the effective interval. No
  broker schedule, timezone, or freshness threshold is supplied by default.
- **Gap handling:** The MT5 adapter validates record integrity and bounds but
  does not classify session gaps. The historical pipeline and
  `ValidatedBarSet` apply the caller's calendar after verifying its provider
  and instrument scope before gap classification; a mismatch raises
  `ValueError`. Empty input cannot establish provider identity. The current
  MT5 source label `mt5` identifies the adapter type, not a unique broker/server;
  a production broker calendar requires a stable provider/broker source
  identity. Known closures remain distinguishable from missing
  bars during open intervals and unknown calendar coverage; unknown coverage
  fails closed. The legacy `expected_open` callback remains unversioned
  compatibility input and cannot satisfy calendar-aware freshness.
- **DST:** Local session boundaries are converted with IANA timezone rules.
  Ambiguous local boundaries require an explicit `fold`; nonexistent local
  boundaries fail instead of being silently normalized. A local midnight to
  the next local midnight is a valid full-day session and may have 23 or 25
  elapsed UTC hours across DST transitions.
- **D1 semantics:** With an explicit `SessionCalendar`, D1 bars are local
  calendar days in that calendar's timezone, from local midnight to the next
  local midnight; elapsed duration may be 23, 24, or 25 UTC hours. Without a
  calendar, legacy D1 validation and completion remain fixed 24-hour UTC
  intervals. The `one-timeframe-duration` v1.0.0 freshness limit remains a
  fixed 24-hour wall-clock age and is not changed by this interpretation.
- **Freshness:** Preserve `one-timeframe-duration` v1.0.0 unchanged. Add
  `calendar-open-age` v1.0.0 with explicitly configured maximum wall-clock and
  open-market ages. The hard wall-clock limit always applies; unknown calendar
  intervals fail closed, and no reopening grace interval is inferred.
- **Identity:** Calendar ID, version, scope, provenance, timezone, effective
  and coverage intervals, full configuration fingerprint, freshness
  policy/configuration, both age measures, and diagnostics participate in
  calendar-aware multi-timeframe input identity. Validated bars retain the
  complete deterministic calendar identity.
- **Compatibility and limits:** No production broker calendar is configured
  or validated. Synthetic calendar tests explicitly identify synthetic
  providers, instruments, and provenance; they establish contract behavior
  only. Actual schedule authority, schedule data, and numeric freshness limits
  require explicit operational configuration and separate verification.
- **Scope:** This decision changes only Phase 1 data-quality and freshness
  boundaries. It does not add studies, trading behavior, or persistence schema.

## ADR-015: Bounded retries and connection timeout for the read-only MT5 adapter

- **Decision:** Bound MT5 initialization to three attempts, pass the supported
  60-second timeout to `initialize()`, and use bounded exponential backoff.
  Raised client exceptions during historical bar/tick calls are retried at most
  three times; empty responses and malformed data are not retried.
- **Error behavior:** A terminal `False` initialization result is checked
  against `last_error()`. Failed or interrupted initialization retains a
  cleanup obligation, and shutdown errors remain visible. Retry exhaustion
  surfaces the final chained adapter error.
- **Timeout limitation:** MT5 historical range functions expose no request
  timeout parameter. The adapter does not move those calls to abandonable
  threads or claim a hard request deadline; a synchronous call can exceed the
  retry bound before returning. This remains an operational limitation until a
  cancellable client mechanism is available.
- **Scope:** No terminal connection is made by tests; all retry and timeout
  behavior is validated with deterministic fake clients. No trading API is
  added.

## ADR-016: Quote and tick event identity in SQLite schema v4

- **Decision:** Preserve separate tick/quote events even when instrument,
  source, and UTC timestamp are identical. Schema v4 gives each stored event an
  append-order row ID and a unique hash of its market identity fields; retrieval
  timestamps are excluded so rereading an identical event remains a duplicate.
- **Reason:** MT5 can report distinct updates with the same millisecond
  timestamp. The prior composite primary key rejected one of those updates and
  could lose market information.
- **Migration:** Rebuild both event tables inside the existing `BEGIN IMMEDIATE`
  schema-upgrade transaction, preserve every prior row and its insertion order,
  and recreate source lookup indexes. A failed migration rolls back table
  changes and the schema version together.
- **Scope and limits:** This is event deduplication, not a guarantee that all
  provider event IDs are globally unique. Persistence methods append only;
  direct database edits remain possible and are outside the application API.

## ADR-017: Fail-closed pipeline-to-study execution

- **Decision:** `run_pipeline_study` retrieves `ValidatedBarSet` input at the
  study request's UTC cutoff and invokes `StudyRunner` only on success. Typed
  historical quality, empty-history, and provider-access failures are mapped
  to the corresponding non-VALID `StudyStatus` and persisted without
  computation. Provider source and stable reason codes are retained as audit
  evidence; unclassified exceptions propagate.
- **Status mapping:** `DataStatus` and `StudyStatus` share the current values
  except `VALID`/`AVAILABLE`; only valid data reaches normal study execution.
  The runner preserves each non-VALID data status one-to-one. The bridge emits
  `INSUFFICIENT_DATA` for empty/quality-short history and `ERROR` for provider
  access failures.
- **Scope:** This bridge does not add retries to studies, suppress invalid
  request/configuration errors, or make multi-component persistence atomic.

## ADR-018: Descriptive RSI and single-horizon ROC

- **Decision:** Implement Wilder RSI level and one-completed-bar change and
  percentage ROC, with explicit periods and formula version 0.1.0. RSI requires
  `period + 2` bars and ROC `period + 1`; exact formulas and zero policies are
  specified in the study catalogue.
- **Consequences:** Each run uses validated completed bars from one timeframe.
  ROC horizons use distinct configurations and cache keys, without an assumed
  normalization. RSI direction describes RSI movement, not market regime or
  overbought/oversold status. Both are descriptive, not validated strategies;
  arithmetic failures use the runner's fail-closed error path.
