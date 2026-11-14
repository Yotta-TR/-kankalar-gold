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
