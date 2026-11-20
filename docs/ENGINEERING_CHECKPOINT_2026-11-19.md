# Engineering checkpoint — 2026-11-19

## Phase 2G

- Added descriptive `structure.range_failed_breakout` v0.1.0: completed
  close-confirmed candidates use a frozen preceding range and a bounded later
  close reclaim. Wick reclaims remain a separate sweep definition.
- Added immutable failed-breakout events, transactional schema v11, event
  observations, cache restoration, and append-only replacement guards.
- No volume strength, predictive score, or trading recommendation is provided.
- Local Python 3.14.2: 1,010 tests passed; the safety subset passed 18 tests
  (22 deselected). Ruff lint/format, mypy and diff checks passed. Python 3.11
  and 3.12 require CI verification.

## Phase 2F

- Added descriptive `structure.liquidity_sweep` v0.1.0 with explicit swing,
  zone and minimum overshoot configuration. Each candle uses only zone membership
  available at its open; completed wick reclaims are selected independently
  per side, without inferred intrabar order or volume-based strength.
- Added immutable sweep events, referenced zone storage, transactional schema
  v10, cache restoration and append-only/rowid replacement guards. Synthetic
  tests cover point-in-time selection, D1 DST, migrations and pipeline safety.
- Alternative structural definitions and predictive validation remain TBD.
  No trading signal or recommendation is added.
- Local Python 3.14.2: 984 tests passed; the safety subset passed 18 tests
  (22 deselected). Ruff lint/format, mypy and diff checks passed;
  Python 3.11 and 3.12 require CI verification.

## Phase 2E

- Added descriptive `structure.sr_zones` v0.1.0, with explicit swing windows,
  pivot lookback and basis-point tolerance. Independent high/low complete-linkage
  clusters require two confirmed members, retain reference IDs and never use
  volume. Volume-based strength is unavailable and unimplemented.
- Added immutable zone identities and transactional SQLite schema v9 with
  append-only records, observation links, rowid-aware replacement guards and
  cache restoration. Membership changes produce new records; older observations
  remain unchanged.
- Structural-study open decisions, including zone lifecycle, strength and
  ranking, remain listed in the study catalogue. No signal or strategy is added.
- Local Python 3.14.2 validation: full pytest 950 passed; safety subset
  18 passed (22 deselected); Ruff lint/format, strict mypy and whitespace
  checks passed. Python 3.11 and 3.12 require CI verification.

## Reconciled starting state

- The working tree began on local `main` at `7fad403a376bd1c99a266b0887cedf4e59ec140d`;
  `origin/main` was `72d73f42987f1592228abe61c03cf8c89bc901b8`, leaving the local
  DMI/ADX commit one commit ahead.
- The `.git/index` SHA-256 matched the supplied historical fingerprint
  `5536888ae5e91b66a4e3b724cbac027894cf85eaad2737c3a5f0cf2035bc24d0`.
- Staged and unstaged SessionCalendar/data-quality changes differed at start.
  Recovery snapshots of the non-protected changes were saved outside the
  repository before reconciliation. Existing review exports were preserved.
- Integrated work is being carried on
  `feature/wp1-wp5-integration-20261119`, created from the observed local HEAD.
- The pre-existing DMI/ADX commit remains part of the feature branch. The
  reviewed integration commits are `54e0d11` (calendar), `e555210` (MT5 and
  fail-closed study integration), and `f491431` (SQLite and documentation).
- The feature branch was pushed to `origin` and PR #1 was opened against
  `main`. No GitHub status checks were reported when this checkpoint was
  recorded; local validation results are listed below.
- All intended non-protected changes are committed, with no unstaged tracked
  changes. Pre-existing protected staged entries and untracked review exports
  remain preserved and are not part of these commits.

## Work-package record

### WP1 — Session Calendar

- Reviewed scope/provenance validation, weekly and overnight sessions,
  holidays/exceptional closures, DST folds and gaps, half-open interval
  classification, isolated-bar coverage, D1 local-midnight completion,
  bounded `latest_complete_bar` search, fail-closed unknown coverage, and
  calendar-aware freshness.
- Confirmed tests use synthetic schedules and no production broker calendar is
  configured. Calendar scope and full bar-interval coverage are enforced.
- Staged and working-tree versions were reconciled after validation; the
  integration index is intended to contain the same reviewed non-protected
  source as the working tree.

### WP3 — MT5 reliability

- Kept the adapter injectable, read-only, symbol-explicit, and health-checked.
  Initialization now has three bounded attempts, passes MT5's 60-second
  initialization timeout, cleans up failed sessions, and rejects malformed
  non-boolean success results. Raised historical-call exceptions receive at
  most three attempts with bounded exponential delay.
- MT5 history methods have no request-timeout argument; a blocked synchronous
  call cannot be safely interrupted by this adapter. This is documented as an
  operational limitation, not presented as a hard end-to-end deadline.
- No live terminal or broker was used.

### WP2 — Quote and tick validation

- Quote quality checks cover non-finite/non-positive/crossed bid-ask, future or
  stale timestamps, timestamp-millisecond consistency, invalid last, and
  explicit maximum spread.
- MT5 tick records validate timestamp and range, non-negative prices, crossed
  markets, tick flags, changed-field consistency, exact duplicate events, and
  chronological order. Distinct updates with equal timestamps remain valid;
  empty tick history is represented by a typed error.
- Quotes/ticks still lack a shared validated study-input carrier; quote age and
  spread thresholds must be explicitly supplied to `validate_quote`.

### WP4 — Failure integration

- Added `run_pipeline_study`: only cutoff-bound `ValidatedBarSet` input reaches
  computation. Historical-quality, empty-history, and typed provider failures
  become persisted non-computing study results with status, source/reason
  evidence, and check time. Unclassified request/programming errors propagate.

### WP5 — SQLite

- Reviewed explicit transactions, migration rollback, append-oriented writes,
  snapshot-keyed cache identity, and strict cache-row validation.
- Schema v4 rebuilds quote/tick tables transactionally with stable event hashes
  that exclude retrieval time. Exact repeats remain conflicts; distinct events
  sharing a UTC timestamp are stored in append order. Migration and rollback
  tests preserve old rows.
- Persistence methods are insert-only, but direct SQL edits are not blocked by
  database triggers.

### WP6 — Documentation

- Updated README, study catalogue, architecture decisions, operations guidance,
  and data dictionary. This checkpoint records implementation state and the
  remaining operational/research limits.

## Validation

- Full suite: `pytest -q` — **749 passed**.
- Focused calendar, adapter, integration, storage, no-lookahead, and trading
  safety suite — **272 passed**.
- `ruff check .` — **passed**.
- `ruff format --check .` — **85 files already formatted**.
- `mypy src` — **passed; 47 source files checked**.
- Staged and unstaged `git diff --check` (excluding protected review files) —
  **passed**.
- Static trading-safety checks and no-lookahead regressions are included in the
  focused test run.

## Remaining risks and next step

- The broker-specific calendar, timezone, holidays, provider identity, and
  freshness thresholds remain unconfigured and unverified.
- MT5 behavior has only been tested with deterministic fakes. A separate
  reviewed read-only terminal environment is required before any terminal
  initialization.
- MT5 history calls have bounded retries but no enforceable per-call timeout.
- No predictive performance claim is made; studies remain descriptive research
  components. Real order execution, unattended trading, and Telegram remain
  prohibited/out of scope.
- Next: begin Phase 2 study work only after reviewing the integrated commits
  and preserving the research/no-lookahead acceptance gates.

## Phase 2B — 2026-11-19

- Added descriptive MACD and prior-completed-bar Donchian studies with explicit
  period configurations, fail-closed arithmetic, and no new defaults or schema.
  MACD components besides the histogram remain evidence strings; Donchian
  position is not clipped and zero-width positions are unavailable.
- Added synthetic boundary, numerical, persistence, pipeline and no-lookahead
  checks, including runner-level RSI overflow regression. These studies are
  not trading signals or predictive claims; see ADR-019 and the catalogue.

## Phase 2C — 2026-11-19

- Implemented `structure.swings` v0.1.0 with required positive integer
  `left_bars` and `right_bars`, strict unique extrema, independent high/low
  evaluation, and delayed confirmation timestamps. A pivot timestamp is the
  opening time of its candle; `known_at_utc` is the completion of its last
  right-hand confirmation candle.
- Added immutable typed event collections to study outputs/results and a
  contextual runner hook carrying cutoff, timeframe, validated calendar
  timezone and stable calendar identity, snapshot ID, and input-window
  metadata. D1 confirmation uses local-day completion and therefore honors
  23-, 24-, and 25-hour calendar days where applicable; without a calendar it
  uses fixed 24-hour UTC days. Calendar-aware event IDs include the context
  identity so different confirmation instants do not conflict in append-only
  event storage.
- SQLite schema v5 adds append-only study-event identities and result
  observations. Event identities exclude retrieval/snapshot time. Result,
  events, and observation links persist atomically; identical observations
  reuse event identities and conflicting payloads fail closed. Cache hits
  restore structured events. Schema v6 includes calendar identity in cache
  lookups while keeping `data_snapshot_id` limited to bar data. Schema v7 adds
  insert guards because SQLite `INSERT OR REPLACE` could otherwise bypass the
  update/delete triggers; migration preserves existing event records.
- Regression coverage confirms v6 databases gain the replacement guards and
  that event identities and observation links reject replacement as well as
  updates and deletes.
- Post-review local validation (Python 3.14.2): `pytest -q` — 902 passed;
  safety subset — 18 passed, 22 deselected; `ruff check .`, `ruff format
  --check .`, `mypy src` (52 source files), and intended-diff whitespace
  checks — passed.
- BOS/CHoCH, zones, equal-level treatment, prominence, and other downstream
  structure choices remain **[TBD]**. No predictive value or trading approval
  is claimed. See ADR-020 and the study catalogue.

## Phase 2D — 2026-11-19

- Implemented `structure.bos_choch` v0.1.0, using confirmed swings known at
  candle open and strictly crossed completed closes. The nearest crossed level
  wins; equal-price ties use latest confirmation, then pivot and ID. All crossed
  eligible levels are consumed. Bias starts unset and changes only on emitted
  breaks; see ADR-021 for initial, continuation and reversal classifications.
- Added frozen structure-break records, transactional schema v8, append-only
  observations, cache round trips, payload-conflict rollback, and rowid-aware
  replacement guards for both swing and break records. D1 completion uses the
  validated calendar's timezone, with the existing fixed UTC fallback.
- During the draft, a focused test initially imported the original checkout
  through the editable environment instead of the isolated worktree; using
  `PYTHONPATH=src` in the worktree fixes import resolution. Migration assertions
  expecting schema 7 were updated to 8. Mypy originally inferred a mixed
  expanded tuple as `object`; an explicitly typed union list preserves the
  element type. A safety scan also rejected `getattr`; explicit field access
  fixed it. The earlier cutoff fixture failed the quality gate before reaching
  the cutoff gate; validation time is now set after completion to test the
  intended boundary directly.
- The result depends on the supplied history's start; keep it consistent when
  replaying or extending a run. The implementation is descriptive research
  only; zones, trading, and predictive claims remain out of scope.
- Local validation on Python 3.14.2: 934 pytest tests passed; the explicit
  safety subset passed 18 tests (22 deselected); Ruff lint and format, mypy
  (53 source files), and working-tree whitespace checks passed. Python 3.11
  and 3.12 require the separate CI matrix.
