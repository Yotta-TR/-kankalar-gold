# Study Catalogue

Status: This catalogue documents research candidates. `volatility.atr`,
`trend.ma_slope_order`, and `trend.adx_dm` are implemented as descriptive
studies; listing or implementation does not imply predictive approval or
ensemble activation.
This catalogue organizes the research candidates of Sections 5.1–5.6 of
[`KANKALAR_GOLD_AI_MASTER_SPEC_v0.1.md`](../KANKALAR_GOLD_AI_MASTER_SPEC_v0.1.md)
(the "master spec", which remains canonical) into implementable specification
entries. Where this file and the master spec differ, the master spec wins.

## 1. How to read this catalogue

Provenance is marked with three kinds of labels. Labels are applied where a
statement's origin matters; they are not attached to every individual sentence.
Unlabelled text is organizational, or restates the verified code facts in §3.

| Label | Meaning |
|---|---|
| **[SPEC §x]** | Reference to the master-spec section `x` that states the requirement. |
| **[TBD]** | `TBD — requires an explicit research/design decision`. Unresolved; nothing is assumed. |
| **[SUGGESTION]** | A non-binding recommendation or question. It is not an approved requirement. |

Being listed here does **not** by itself approve implementation, activation in
the signal ensemble, or any decision use. Activation is conditional on evidence
[SPEC §5 intro].

## 2. Research safeguards (apply to every study)

- Research and decision support only. No automated trading, no MT5 order
  execution [SPEC §2].
- No promise of profitability. No manufactured confidence or probabilities;
  probability display stays disabled until calibration/sample gates pass
  [SPEC §19 `allow_probability_display`].
- A study output is **descriptive** until its predictive contribution is
  separately validated [SPEC §6].
- Correlated studies must be grouped or regularized; agreement is never counted
  as independent votes [SPEC §6].
- No lookahead, repainting, or future-data leakage. Signals use completed bars
  only, unless a study documents itself as tick-based and records exact event
  time [SPEC §3, §6].
- Broker tick volume is not consolidated global gold volume. Unlike venues are
  never combined without explicit normalization and source labels [SPEC §4.5].
- COMEX/futures data (GC, MGC) and broker gold (CFD) data remain separate
  sources/instruments in storage and features. Futures contract sizes are never
  copied into broker CFD configuration [SPEC §4.3].
- Macro history respects actual publication timestamps (point-in-time); revised
  values are never back-filled as if known earlier [SPEC §4.4].
- A missing depth feed is `UNAVAILABLE`, not zero imbalance [SPEC §4.5]. Missing
  values are never silently converted to zero [SPEC §6].
- Timeframe combinations are not optimised on the final holdout [SPEC §3].
- Terms such as "Smart Money", "institutional footprint", "liquidity grab", or
  "order block" are not evidence by themselves [SPEC §5.2].
- `NO_TRADE` and insufficient evidence are valid outcomes
  [SPEC §19 `default_no_trade_on_uncertainty`].

## 3. Verified current code capabilities

Checked against the repository (not assumed):

| Area | Verified fact |
|---|---|
| `StudyResult` (`core/contracts.py`) | Frozen dataclass with fields `study_id`, `version`, `instrument`, `timeframe`, `as_of_utc` (normalized to UTC), `status`, `state`, `value`, `normalized_value`, `direction`, `uncertainty`, `inputs`, `evidence`, `limitations`, `data_snapshot_id`, `provenance` (structured, defaulted; added in Phase 1B.3). |
| `StudyStatus` | `AVAILABLE`, `INSUFFICIENT_DATA`, `UNAVAILABLE`, `STALE`, `ERROR`. |
| `Direction` | `BULLISH`, `BEARISH`, `NEUTRAL`, `NOT_APPLICABLE`. |
| Nullable fields | `state`, `value`, `normalized_value`, `uncertainty`, `data_snapshot_id` are `None`-able, so "missing" is distinct from `0.0`. |
| `inputs` typing | Flat `dict[str, str \| int \| float \| bool \| None]`; nested structures are not representable. |
| Not in the contract | No dedicated fields for retrieval timestamp, code/Git revision (only the study `version`), or posterior probabilities. Source and input window are carried in the structured `provenance` field; the rest must travel via `inputs` or `evidence` **[TBD]**. Changing the contract is out of scope here. |
| `DataStatus` (`core/contracts.py`) | `VALID`, `STALE`, `INSUFFICIENT_DATA`, `UNAVAILABLE`, `ERROR`. This is the *data* status, distinct from `StudyStatus` (no `VALID` there; `AVAILABLE` instead). |
| Bars/ticks/quotes | `MarketBar` carries instrument, timeframe, UTC timestamp, OHLC, `volume`, `tick_volume`, `real_volume`, `spread_points`, `source`, `retrieved_at_utc`. `MarketTick` and `MarketQuote` carry bid/ask/last, `source`, `retrieved_at_utc`. |
| Timeframes | `HistoricalDataPipeline` supports `M1, M5, M15, H1, H4, D1`; retrieval is bounded to 366 days. |
| Fail-closed bar ingestion (`data/pipeline.py`) | `HistoricalDataPipeline.retrieve_bars` and `latest_complete_bar` validate **bars only** and raise `HistoricalDataQualityError` (carrying the `DataQualityReport`) for any status other than `VALID`. |
| Quotes and ticks | `validate_quote` reports non-finite/crossed/non-positive bid/ask, future or stale timestamps, inconsistent milliseconds, invalid last, and excessive spread. The MT5 adapter validates tick timestamp/range, finite and non-negative price values, bid/ask ordering, flags, changed-field consistency, exact duplicate records, and out-of-order events; distinct updates sharing a timestamp are preserved. Empty tick history has a typed missing-data error. Quotes and ticks still do not pass through a shared `ValidatedQuote`/`ValidatedTick` study pipeline; quote freshness/spread thresholds must be explicitly checked by the caller **[TBD]**. |
| Instrument | The logical project identity is `GOLD_USD` [SPEC §19]; the broker symbol is `null` until discovered and confirmed from MT5 [SPEC §19]. It is commonly labelled XAUUSD, but the exact symbol is unresolved. |
| Gap semantics (`data/calendar.py`, `data/quality.py`) | Gaps are classified with an explicit broker/instrument calendar. Known closures are retained as valid-quality diagnostics, open-session missing bars and unknown calendar coverage fail closed. The typed calendar contract has no broker schedule or timezone defaults; all calendar tests use synthetic schedules. |
| Persistence (`storage/sqlite.py`) | `save_study_result` appends one row to `study_results` with `inputs`, `evidence` and `limitations` serialized as JSON (`inputs_json`, `evidence_json`, `limitations_json`). Schema v3 adds `provenance_json` and `cache_format_version`; schema v4 rebuilds quote/tick tables with event identities so distinct updates at one timestamp are retained while exact repeats are rejected. `find_cached_study_result` strictly deserializes and validates the newest cache-eligible row (see ADR-007). Data-record writes are insert-only through the persistence API; schema migrations and each write batch are transactional. Schema version 4. |
| Timeframe field | `StudyResult.timeframe` is a required `str`; it has no defined representation for non-bar (macro/event/weekly) studies (see §13). |
| Study execution | `studies/runner.py` executes one timeframe per request and returns one `StudyResult`; a registration may support multiple independent timeframe runs. `compute_bar_snapshot_id` generates each run's timeframe-specific snapshot. |
| Pipeline failure integration | `studies.pipeline_execution.run_pipeline_study` runs only validated bars; known quality, empty-history, and typed provider failures become persisted non-computing `StudyResult`s with status/reason/source evidence. Other request/programming errors still propagate. |
| Multi-timeframe coordination | `studies/multitimeframe.py` defines requests and complete preflight; `studies/multitimeframe_coordinator.py` executes the independent runs in canonical order after preflight. |
| Runner preparation | `StudyRunner.prepare` validates and normalizes one request/input pair and computes its individual snapshot metadata without cache lookup, computation or persistence. |
| Not present | No configured/verified broker calendar schedule, no macro/COT/futures adapters, no volume-profile or order-book data. |

### 3.1 Multi-timeframe coordination

- A coordinator request uses one instrument and common normalized UTC cutoff,
  and may select any non-empty subset of M1, M5, M15, H1, H4 and D1.
- Each requested timeframe has exactly one tagged input: a validated bar set
  or an explicit data failure. Multiple executions may reuse that timeframe's
  input. Source strings must match across all valid bar sets.
- The fixed `one-timeframe-duration` v1.0.0 freshness policy records the actual
  maximum age for every supported timeframe. The limit is inclusive and has no
  automatic weekend or closure exemption. Candle completion time is open time
  plus timeframe duration.
- A separate `calendar-open-age` v1.0.0 policy requires explicit wall-clock
  and open-market age limits plus one explicit `SessionCalendar`. Its manifest
  records the calendar identity/configuration fingerprint, policy configuration,
  wall-clock age, open-market age, and unknown-coverage diagnostics. It has no
  post-reopening grace period; unknown calendar coverage fails closed.
- `SessionCalendar` represents local weekly sessions, overnight sessions,
  holidays, exceptional UTC closures, and separate effective and coverage
  intervals. Effective dates define when a schedule applies; coverage dates
  define where its schedule information is known. Every calendar explicitly
  names its provider, instrument, provenance source/reference, and IANA
  timezone, and validated inputs cannot reuse it for another provider or
  instrument. IANA timezone rules are used for DST. Ambiguous local schedule
  boundaries require an explicit fold; nonexistent boundaries fail rather
  than being guessed.
- Calendar scope is checked before gap classification in both raw and validated
  paths; a mismatch raises `ValueError`. Empty input can be checked against an
  explicitly requested instrument but cannot establish a provider identity.
  The MT5 adapter's current source value `mt5` identifies the adapter type, not
  a unique broker/server. A production broker calendar requires a stable,
  explicitly configured broker/provider source identity.
- D1 bars use local midnight-to-midnight boundaries only when validated with an
  explicit calendar; these days may span 23, 24, or 25 UTC hours. Without a
  calendar, existing D1 behavior remains fixed 24-hour UTC intervals. The
  `one-timeframe-duration` v1.0.0 freshness limit remains fixed at 24 hours.
  Calendar-scoped D1 `latest_complete_bar` searches a configurable 1–366 UTC
  day lookback (default 366), excludes the current local day, and fails closed
  if a trailing gap contains open time or unknown calendar coverage. With no
  completed candidates, it returns no bar only when the search interval is
  fully known closed. Empty history cannot establish a provider identity.
  Every calendar-validated bar's entire half-open interval must be covered.
- Legacy `expected_open` callbacks remain supported for historical bar
  validation, but they have no stable identity/version and cannot satisfy the
  calendar-aware freshness policy. No real broker schedule has been validated;
  calendar tests use synthetic schedules only.
- Preflight prepares every single-timeframe request and rejects normalized
  duplicate execution identities before any component execution or persistence.
- The canonical input identity excludes final study-result statuses. The
  separately defined result identity includes final component records/statuses.
- Canonical manifests use sorted-key compact JSON, canonical UTC timestamps,
  integer-microsecond ages, deterministic component ordering, and versioned
  SHA-256 identity prefixes.
- The coordinator records explicit failures through the runner and converts
  stale validated input into a `STALE` failure without computing it. Individual
  results use the runner's cache and persistence; a later persistence failure
  propagates and does not roll back earlier results. No aggregate is persisted.
- No evidence is combined into a score, probability, direction, or recommendation.

## 4. Common conventions

### 4.1 Study ID convention **[SUGGESTION]**

`<group>.<snake_case_name>`, lowercase, versioned separately through
`StudyResult.version`. Groups: `trend`, `structure`, `volatility`, `regime`,
`liquidity`, `macro`. The spec's example `volatility.atr_regime` [SPEC §6] is
the basis for the dotted pattern; the IDs below are catalogue proposals, not
spec-mandated names.

### 4.2 Common output contract

All studies emit `StudyResult` [SPEC §6]. Per-study entries only list what is
specific.

| Contract field | Rule |
|---|---|
| `status` | `AVAILABLE` only when inputs are `VALID` and the warm-up is satisfied. `INSUFFICIENT_DATA` (not enough history), `UNAVAILABLE` (source/feature absent), `STALE` (inputs too old), `ERROR` (failure/malformed) [SPEC §6]. |
| `value` / `normalized_value` / `uncertainty` | `None` when not computable; never substituted with `0.0` [SPEC §6]. |
| `direction` | `BULLISH`/`BEARISH`/`NEUTRAL` only where meaningful, otherwise `NOT_APPLICABLE` [SPEC §6]. |
| `inputs` | Records configuration/parameters; input window is recorded in provenance [SPEC §6]. |
| `evidence`, `limitations` | Human-readable; limitations are mandatory (e.g. tick-volume caveat) [SPEC §6]. |
| `data_snapshot_id` | Bar input snapshots use `compute_bar_snapshot_id`; explicit data failures have no snapshot [SPEC §6]. |
| `as_of_utc`, `instrument`, `timeframe` | Always populated, UTC [SPEC §3, §6]. |

### 4.3 Data-quality dependency mapping

Inputs must pass the data-quality gate [SPEC §4.6] before a study may run. A
quality failure blocks signal creation or downgrades the system to `DEGRADED`
[SPEC §4.6]; no symbol, source or timeframe is silently substituted
[SPEC §4.6].

Mapping of upstream `DataStatus` to `StudyStatus`, implemented for caller-supplied
`DataFailure` objects in `StudyRunner.run_data_failure` (the spec requires the
statuses to stay distinct but does not define this table):

| Upstream `DataStatus` | Study outcome |
|---|---|
| `VALID` | May compute; `AVAILABLE` if warm-up is also met. |
| `INSUFFICIENT_DATA` | `INSUFFICIENT_DATA` |
| `UNAVAILABLE` | `UNAVAILABLE` |
| `STALE` | `STALE` |
| `ERROR` | `ERROR` |

The runner maps a caller-supplied `DataFailure` one-to-one to the same-named `StudyStatus`. The coordinator also maps stale validated bars to a `STALE` failure without computing. The bar pipeline does not automatically produce a `DataFailure`; invalid data never reaches `compute`.

The existing fixed coordinator freshness policy has no weekend or closure
exemption. The separate calendar-aware policy is implemented for explicit
calendar inputs; the actual broker schedule, timezone, exceptional closures,
freshness thresholds, and calendar source remain **[TBD]**.

### 4.4 Common closed-bar / point-in-time behavior

- Only completed bars; a bar's final high/low/close/volume is never used before
  it closes [SPEC §3]. Tick-based studies must document that and record exact
  event time [SPEC §3].
- Macro/positioning inputs are keyed by publication time, not observation
  period [SPEC §4.4].
- Intermediate state must be reproducible from data available at `as_of_utc`;
  later bars must not change an already emitted result (no repainting)
  [SPEC §6].

### 4.5 Common test requirements

Phase 2 acceptance requires boundary cases, warm-up windows, missing bars, and
no-lookahead checks, plus documentation and a version for each study
[SPEC §16 Phase 2]. Every study therefore needs:

1. **Unit tests:** deterministic synthetic data; boundary cases; warm-up
   (insufficient-data status); missing bars; missing value stays `None`;
   each non-`AVAILABLE` status.
2. **No-lookahead test:** result at time *t* is unchanged when data after *t*
   is appended or altered.
3. **Integration tests:** the study consumes only validated inputs through the
   source-specific path appropriate to its data: bars through the fail-closed
   `HistoricalDataPipeline`; quotes/ticks through their existing validation
   and ingestion capabilities (§3); external macro, positioning, futures,
   volume and order-book data through a validated-input path or adapter that
   does not yet exist **[TBD]**. Additionally, a persistence-write test:
   `save_study_result` stores the row and the stored fields and JSON columns
   (`inputs_json`, `evidence_json`, `limitations_json`) are verified by direct
   inspection of the database. A strict, cache-oriented read with
   deserialization (`find_cached_study_result`) exists; a general result-query
   API does not **[TBD]**.
4. Study-specific cases listed in each entry. Expected numeric values are
   **[TBD]** until a formula is approved.

### 4.6 Common chronological out-of-sample evaluation

Per [SPEC §11, `docs/BACKTEST_PROTOCOL.md`]: freeze data and definitions first;
train → validation → untouched holdout → later paper period, strictly ordered
in time; fit transforms/calibrators only on earlier periods; no tuning on the
holdout; report limitations. A study remains descriptive until its predictive
contribution is validated this way. Study-specific metrics and targets are
**[TBD]** (target definition belongs to [SPEC §8.2]).

### 4.7 Defaults inherited by every entry

Unless an entry overrides it: *Warm-up* = **[TBD]**; *Timeframe* = chosen from
the plan in [SPEC §3] (D1 context, H4 regime, H1 bias, M15 setup, M5
confirmation, M1 optional diagnostics, only when reliable data exists);
*Parameters* = **[TBD]** unless an example value is quoted; *Priority* as
assigned; *Formula* is not invented.

## 5. Implementation priority classes

| Class | Meaning | Why / dependencies |
|---|---|---|
| **FOUNDATION** | Reusable infrastructure required before any study runs. | The Study Execution Engine must exist before a large collection of studies. |
| **BASELINE** | Simple, transparent studies using only closed OHLC bars. | Depend on the core execution prerequisites (§5.1) and the existing validated bar pipeline; no session calendar required. |
| **STRUCTURE** | Market-structure and session studies. | Need precise operational definitions (spec requires exact event rules); session-based studies also need a session calendar. |
| **DATA_DEPENDENT** | Need reliable volume, order-book, macro, futures or positioning sources. | Depend on adapters that do not exist yet [SPEC §4.3–4.5]. |
| **ADVANCED** | Statistical/regime models. | Need further research, validation, and a transparent baseline first [SPEC §5.4]. |

### 5.1 FOUNDATION items (not studies)

Dependencies are separated into three tiers.

**A. Core execution prerequisites** (needed before any study is computed):

| ID | Item | Status today |
|---|---|---|
| F1 | Study Execution Engine (run studies on validated, closed-bar data; emit `StudyResult`) | Single-timeframe `StudyRunner` and independent multi-timeframe coordinator implemented (ADR-007, ADR-011); scheduling remains open |
| F2 | Reproducible data-snapshot identity and provenance | Implemented: `compute_bar_snapshot_id` and structured `StudyResult.provenance`; Git/application revision tracking deferred |
| F3 | Configuration/version handling (study config and version registry) | Implemented: `StudyRegistry`, scalar config validation, explicit study versions |
| F4 | No-lookahead safeguards (test harness) | Runtime cutoff protections and a reusable test-only point-in-time harness are implemented (ADR-008). Pipeline `end_utc` is an inclusive completion cutoff; provider overreach is rejected, and the runner requires the carrier cutoff to match `as_of_utc`. Raw `retrieve_bars` returns no cutoff provenance; use `retrieve_validated_bars` for studies. |
| F5 | Mapping from `DataStatus` to `StudyStatus` (§4.3) | Implemented for a caller-supplied `DataFailure` (`run_data_failure`). Not integrated: the pipeline does not produce a `DataFailure` and `HistoricalDataQualityError` is not converted into a result automatically |

**B. Conditional dependencies** (only for studies that need them):

| ID | Item | Applies to | Status today |
|---|---|---|---|
| C1 | Session/market calendar (`expected_open`) | Only session- or session-boundary-dependent studies (session levels, opening range, session VWAP, relative volume by session/time of day, and any study whose definition depends on closures). **Not** required for ordinary OHLC-bar studies such as RSI or ATR. | Hook exists; no calendar |
| C2 | Source-specific input validation | Quote/tick studies (e.g. spread), volume, order-book, futures, macro and positioning studies | Quote validator exists; tick, volume, book and external-data validation/adapters do not **[TBD]** |

**C. Downstream decision/evaluation prerequisites** (not required to compute a
descriptive study):

| ID | Item | Needed for | Status today |
|---|---|---|---|
| D1 | Correlated-study grouping [SPEC §6] | Combining results as decision evidence | Not built |
| D2 | Chronological evaluation infrastructure [SPEC §11] | Claiming predictive contribution or activation in the ensemble | Not built |

A descriptive study can be computed and stored once tier A (plus any
applicable tier B items) exists; its output stays descriptive until tier C is
satisfied. Group prerequisite statements below use these IDs.

## 6. Group 1 — Trend and momentum [SPEC §5.1]

Common to the group: type *descriptive* (contextual use is **[TBD]**);
input: closed OHLC bars from the verified broker gold instrument (logical identity `GOLD_USD`; commonly labelled XAUUSD, exact broker symbol to be discovered and validated through MT5), via the fail-closed `HistoricalDataPipeline`;
priority **BASELINE** (simple, transparent, bars only); leakage risk: indicator
smoothing must use only bars up to *t*; known limitation: the spec says these are
descriptive features, not universal trading rules [SPEC §5.1]; trend studies
are correlated with each other and must be grouped (§2).

| ID | Candidate | Spec definition | Parameters (spec-supported) | Output/states |
|---|---|---|---|---|
| `trend.ma_slope_order` | SMA slope and ordering | Descriptive fast/middle/slow SMA ordering and fast-SMA slope | v0.1.0 uses explicit periods, lookback and flat threshold; no defaults | Nine ordering/slope states; jointly aligned up/down gives `BULLISH`/`BEARISH`, otherwise `NEUTRAL` |
| `trend.adx_dm` | ADX and directional movement | Named only; formula and period remain unspecified in the master spec | v0.1.0 requires explicit `adx_period >= 2`; no default | ADX plus DI dominance; see §6.2 |
| `trend.rsi` | RSI level, slope, regime-aware interpretation | Level and slope; interpretation depends on regime | Period/levels **[TBD]** | Level/slope; regime dependence implies a link to Group 4 **[TBD]** |
| `trend.macd` | MACD | Level, signal relationship, histogram change, zero-line context | Fast/slow/signal **[TBD]** | Components above; states **[TBD]** |
| `trend.roc_momentum` | Rate of Change / momentum | Over multiple horizons | Horizons **[TBD]** | Per-horizon values; `inputs` is flat so horizons are encoded as scalar keys **[SUGGESTION]** |
| `trend.donchian` | Donchian channel position and breakout state | Position within channel; breakout state | Lookback **[TBD]**; whether the current forming bar is excluded must follow closed-bar rule | Position, breakout state **[TBD]** |
| `trend.atr_trend` | Supertrend or other ATR-based trend descriptors | "As candidates, not assumed superior" | ATR period/multiplier **[TBD]** | State **[TBD]**; repainting-prone indicators need explicit tests |

For entries that remain unimplemented, warm-up is **[TBD]** (must emit
`INSUFFICIENT_DATA` before it is met); data-quality dependency: `VALID` bars
for the full lookback; specific tests: warm-up boundary, closed-bar only,
missing bar behavior, no-lookahead; out-of-sample: §4.6, metric **[TBD]**.
Prerequisites: core F1–F5 only; no session calendar (C1) is required. D1/D2
apply only before combining results as decision evidence or claiming
predictive contribution. `trend.atr_trend` additionally depends on the ATR study
`volatility.atr` (§8).

### 6.1 `trend.ma_slope_order` v0.1.0

- **Configuration:** All five fields are required: positive built-in integer
  `fast_period < middle_period < slow_period`, positive built-in integer
  `slope_lookback`, and finite non-negative numeric
  `flat_slope_fraction_per_bar` (booleans excluded). No implicit defaults.
  20/50/200 is only an illustrative research configuration, not a universal rule.
- **Formula:** For closes `C` at the last completed bar `t`,
  `SMA_p(t) = fsum(C[t-p+1:t+1]) / p`. Let `F = SMA_fast(t)`,
  `P = SMA_fast(t-lookback)`, and `delta = F-P`. `value = delta/lookback`;
  `normalized_value = (delta/P)/lookback`, a dimensionless fraction per
  observed bar, not a percentage or probability. No rounding precedes comparison.
  Minimum bars are `max(slow_period, fast_period + slope_lookback)`.
- **States:** Strict `fast > middle > slow` gives ordering `UP`; strict
  `fast < middle < slow` gives `DOWN`; all other cases, including equality, give
  `MIXED`. Normalized slope greater than the explicit threshold gives `UP`,
  less than its negative gives `DOWN`, and otherwise gives `FLAT`, including
  boundary equality. The nine states are `ORDER_UP_SLOPE_UP`,
  `ORDER_UP_SLOPE_DOWN`, `ORDER_UP_SLOPE_FLAT`, `ORDER_DOWN_SLOPE_UP`,
  `ORDER_DOWN_SLOPE_DOWN`, `ORDER_DOWN_SLOPE_FLAT`, `ORDER_MIXED_SLOPE_UP`,
  `ORDER_MIXED_SLOPE_DOWN`, and `ORDER_MIXED_SLOPE_FLAT`.
  Only up/up is `BULLISH`, only down/down is `BEARISH`; other available states
  are `NEUTRAL`, not a claim that the market is sideways. `uncertainty` is `None`.
- **Inputs and safety:** M1, M5, M15, H1, H4 and D1 are independent
  single-timeframe executions. Only trailing validated completed closes affect
  calculations; accepted closure gaps count as observed bars. Positive finite
  subnormal denominators are permitted. Non-positive denominators, non-finite
  arithmetic and overflow cause a runner `ERROR`, retaining the input snapshot.
  Stale and explicit failed data follow the coordinator/runner failure paths.
- **Limitations and tests:** This is a lagging, parameter-sensitive description,
  not a prediction, recommendation or probability. It uses no volume, ATR,
  external data or live connection. Its close-derived outputs are correlated
  with other moving-average and momentum studies; ATR dependence should be
  evaluated, not assumed independent. Synthetic tests cover numerical examples,
  all states, boundaries, configuration, warm-up, numerical failures, data
  quality, no-lookahead, all timeframes, caching and SQLite persistence.

### 6.2 `trend.adx_dm` v0.1.0

- **Canonical boundary:** The master specification names ADX and directional
  movement but does not define the formula, period, zero-denominator behavior,
  or outputs. The choices below are implementation decisions, not additions to
  the canonical specification.
- **Configuration and warm-up:** Exactly one required field,
  `adx_period`, is a built-in integer of at least 2; booleans, floats, missing
  values, and unknown keys are rejected. There is no implicit default or
  maximum. The first ADX is computable at bar index `2 * adx_period - 1`,
  requiring `2 * adx_period` bars. This is a first-computable-output boundary,
  not a convergence or stability guarantee.
- **Formula:** For each adjacent supplied pair, +DM is the positive high
  increase only when it strictly exceeds the downward low move; -DM is the
  positive low decrease only when it strictly exceeds the upward high move.
  Equal positive moves yield zero for both. True Range is the maximum of
  current high-low and the absolute current-high/low to previous-close gaps.
  The first supplied bar contributes no observation. The first `N` DM and TR
  observations are summed with `math.fsum`; later sums use Wilder's
  `S = S - S/N + current` recurrence. DI is `100 * (smoothed_DM / smoothed_TR)`.
  DX is `100 * abs((+DI - -DI) / (+DI + -DI))`, evaluated equivalently from
  scaled DM sums for numerical stability. The first ADX is the mean of the
  first `N` DX values using `math.fsum`; subsequent ADX uses
  `(previous_ADX * (N - 1) + current_DX) / N`. No historical integer rounding
  is applied.
- **Zero policy:** Exact zero smoothed TR with zero DM defines both DIs and DX
  as zero. Positive TR with both DM sums zero also defines both DIs and DX as
  zero; each zero DX participates in ADX seeding and recurrence. ADX decays
  when its computed DX is zero; zero raw DM alone does not force zero DX because
  residual one-sided smoothed DM can preserve DI dominance. Zero TR with
  non-zero DM is an arithmetic invariant failure. No epsilon is used. This
  policy is explicit project behavior and is not exact TA-Lib parity, whose
  zero handling differs.
- **Outputs:** `value` is ADX in `[0, 100]`. `normalized_value` is scaled
  signed DI dominance in `[-1, 1]`, or `None` when both DM sums are zero.
  `state` is `PLUS_DI_DOMINANT`, `MINUS_DI_DOMINANT`, or `DI_BALANCED`;
  direction is respectively `BULLISH`, `BEARISH`, or `NEUTRAL`, based only on
  the smoothed DM comparison. Evidence includes latest ADX, both DIs, DX,
  period, and zero-denominator case with stable numeric formatting. Evidence is
  for human audit, not a substitute for first-class numeric fields; the shared
  result contract has no dedicated +DI and -DI values.
- **Execution and limits:** Supports independent M1, M5, M15, H1, H4, and D1
  runs over validated completed OHLC bars. Adjacent supplied bars define
  observations; accepted closure gaps are not filled with synthetic bars.
  Positive subnormal arithmetic is allowed when finite. Non-finite arithmetic,
  overflow, or violated mathematical bounds follow the runner's `ERROR` path
  with the input snapshot. This is a lagging, parameter-sensitive description,
  not a prediction, strength-threshold classification, or trading
  recommendation. It uses no volume, external data, or live connection and is
  correlated with MA slope/order and ATR-related range information.
- **Tests:** Synthetic tests cover hand-calculated DI/ADX values, DM selection,
  Wilder initialization and recurrence, exact warm-up, zero cases, numerical
  boundaries, all timeframes, closure and unknown gaps, F4 future-mutation
  independence, cache/SQLite behavior, coordinator stale and explicit failures,
  and non-available result fields.

## 7. Group 2 — Market structure and price action [SPEC §5.2]

Common to the group: priority **STRUCTURE**; input: closed OHLC bars (verified broker gold instrument; exact symbol to be
discovered via MT5); the spec requires every concept to have *an operational definition, a
detection algorithm, and out-of-sample results* [SPEC §5.2], none of which exist
yet — so every detection rule below is **[TBD]**. Type: *descriptive*/
*contextual*. Principal risks: repainting (swings are only known after
confirmation), subjective definitions, session-boundary ambiguity, and
session/calendar dependence (C1, only for session-dependent studies).

| ID | Candidate | Spec text | Open decisions |
|---|---|---|---|
| `structure.swings` | Confirmed swing highs/lows | "with non-repainting definitions" | Confirmation rule and delay **[TBD]**; the confirmation timestamp (not the swing bar time) must be the earliest time the result is known **[SUGGESTION]** |
| `structure.bos_choch` | Break of Structure (BOS) and Change of Character (CHoCH) | "with exact event rules" | Exact rules **[TBD]**; depends on `structure.swings` |
| `structure.sr_zones` | Support/resistance zones | "based on repeatable swing/volume criteria" | Zone construction, width, volume use **[TBD]**; volume only if its meaning is characterized (§10) |
| `structure.range_failed_breakout` | Range boundaries and failed breakouts | Named only | Range and failure definitions **[TBD]** |
| `structure.session_levels` | Opening range and session highs/lows | Named only | Session definitions, time zones, DST **[TBD]**; needs C1 |
| `structure.gaps_fvg` | Gap and imbalance descriptors, including Fair Value Gap candidates | "with precise candle definitions" | Candle definition **[TBD]**; gaps must not be confused with weekend/closure data absence |
| `structure.liquidity_sweep` | Liquidity sweep hypotheses around prior highs/lows and equal highs/lows | Described as "hypotheses" | Level, "equal" tolerance, sweep criteria **[TBD]** |
| `structure.candlestick_patterns` | Candlestick patterns | "only where each pattern is formally defined and independently tested" | One sub-study per pattern; none selected **[TBD]** |

Common details: warm-up **[TBD]**; data-quality dependency: contiguous `VALID`
bars; when the session calendar is unknown (gaps reported as
`GAP_SESSION_UNKNOWN`) session-dependent studies should not claim certainty
**[SUGGESTION]**. Tests: non-repainting (a confirmed swing never changes),
boundary candles, equal-level tolerance edge cases, missing bars,
no-lookahead; out-of-sample: §4.6. Prerequisites: core F1–F5; C1 only for
`structure.session_levels` (and any other session-dependent definition adopted
later); BOS/CHoCH and zones depend on swings; D1/D2 as in §5.1 tier C.

## 8. Group 3 — Volatility [SPEC §5.3]

Common to the group: these estimate volatility/risk distributions and **do not,
by themselves, predict direction** [SPEC §5.3]; `direction` is therefore
`NOT_APPLICABLE` **[SUGGESTION]**. Type: *descriptive* (ATR, realized, EWMA,
range) or *statistical* (GARCH family, HAR-RV, change-point). Input: closed
bars (or intraday returns derived from them) from the verified broker gold instrument (exact symbol to be discovered via MT5).

| ID | Candidate | Spec text | Type | Priority |
|---|---|---|---|---|
| `volatility.atr` | True Range and ATR over multiple horizons | Horizons remain **[TBD]**; version `0.1.0` supports M1, M5, M15, H1, H4 and D1 as independent executions, each with explicit `atr_period`. The initial selected run uses 14; the spec example's period 14 is illustrative, not a master-spec default [SPEC §6]. | descriptive | BASELINE |
| `volatility.realized` | Realized volatility from intraday returns | Return sampling frequency **[TBD]** | descriptive | BASELINE |
| `volatility.ewma` | EWMA volatility | Decay parameter **[TBD]** | descriptive | BASELINE |
| `volatility.percentile_clustering` | Volatility clustering and volatility percentile | Window and clustering measure **[TBD]** | descriptive | BASELINE |
| `volatility.range_compression_bbw` | Range expansion/compression and Bollinger bandwidth | Parameters **[TBD]** | descriptive | BASELINE |
| `volatility.garch11` | GARCH(1,1) baseline | Fitting window, refit schedule, distribution **[TBD]** | statistical | ADVANCED |
| `volatility.egarch_gjr` | EGARCH / GJR-GARCH candidates for asymmetric volatility | Candidates only | statistical | ADVANCED |
| `volatility.har_rv` | HAR-RV candidate for multi-horizon realized-volatility dynamics | Depends on `volatility.realized` | statistical | ADVANCED |
| `volatility.change_point` | Change-point detection for abrupt regime changes | Method **[TBD]**; Bayesian variant also appears in §9 | statistical | ADVANCED |

Details: the canonical master-spec requirements are True Range/ATR over multiple
horizons, closed-bar point-in-time behavior, and no directional prediction from
volatility alone [SPEC §§3, 5.3, 6]. Approved initial implementation choices for
`volatility.atr` v0.1.0 are independent M1, M5, M15, H1, H4 and D1 executions,
explicitly supplied positive built-in integer `atr_period`, omitted first-bar
TR (N periods require N+1 bars), and Wilder smoothing seeded by the arithmetic
mean of the first N TR values.
Each run consumes one timeframe only and has its own result, snapshot, and cache
identity; the current set of six supported timeframes does not mean one run
combines them.
The initial selected run configuration uses `atr_period=14`; it is not a
specification-wide default. Output is descriptive price-unit ATR, without state,
normalized value, uncertainty, or trade recommendation. `volatility.atr_regime`
and labels such as `EXPANDING` remain illustrative only. Future horizons and
any additional supported timeframes beyond the six are unresolved. Independent
multi-timeframe runs can be coordinated at a common cutoff (ADR-011).
Other volatility studies retain their listed TBDs; fitted models must use only
data available at `as_of_utc`, and statistical models need a dependency decision
**[TBD]**.
Prerequisites: core F1–F5 (no C1); D2 is required before any predictive claim.

## 9. Group 4 — Market regime detection [SPEC §5.4]

Type: *contextual* (baseline) / *statistical* (advanced). Priority: transparent
baseline **BASELINE→STRUCTURE** boundary is **[TBD]**; advanced candidates are
**ADVANCED**. The spec requires a transparent baseline before advanced models
[SPEC §5.4, §16 Phase 3].

**Required labels (`StudyResult.state`) [SPEC §5.4]:** `BULLISH_TREND`,
`BEARISH_TREND`, `SIDEWAYS`, `HIGH_VOLATILITY`, `TRANSITIONAL`, `UNCERTAIN`.

Rules [SPEC §5.4]: expose posterior probabilities or uncertainty where
possible; do not force a crisp label when evidence is ambiguous (→ `UNCERTAIN`).
The contract has `uncertainty: float | None` but no posterior-probability field
**[TBD]**. Label-selection rules and thresholds are **[TBD]**. Primary
timeframe is H4 [SPEC §3]; D1 provides context.

| ID | Candidate | Role | Notes |
|---|---|---|---|
| `regime.baseline_trend_strength` | Trend strength and slope | Baseline component | May reuse Group 1 outputs; correlation must be handled (§2) |
| `regime.baseline_persistence` | Directional persistence | Baseline component | Definition **[TBD]** |
| `regime.baseline_vol_percentile` | Realized volatility percentile | Baseline component | Depends on `volatility.realized` |
| `regime.baseline_range_efficiency` | Range efficiency / choppiness | Baseline component | Definition **[TBD]** |
| `regime.baseline_cross_tf_disagreement` | Cross-timeframe disagreement | Baseline component | Requires multiple timeframes aligned on closed bars only |
| `regime.hmm` | Hidden Markov Model | Advanced | Candidate only; model-development order governed by [SPEC §8] |
| `regime.markov_switching` | Markov-switching models | Advanced | Candidate only |
| `regime.bayesian_change_point` | Bayesian change-point detection | Advanced | Overlaps `volatility.change_point` |
| `regime.clustering` | Clustering of volatility/return features | Advanced | Feature set **[TBD]** |

The baseline components may be reported individually or combined into one
`regime.baseline` study that assigns the six labels; the composition is
**[TBD]**. Limitations: regime labels are descriptive; unsupervised/latent
models can relabel past states when refit (leakage/repainting risk — only
filtered, point-in-time estimates are acceptable **[SUGGESTION]**); component
correlation. Tests: each label reachable on synthetic data, ambiguous input →
`UNCERTAIN`, cross-timeframe closed-bar alignment, no retroactive relabelling.
Out-of-sample: §4.6; stability of labels over time is a candidate metric
**[SUGGESTION]**. Prerequisites: Groups 1 and 3 baselines and core F1–F5; D1 (correlation
grouping) before combining components as evidence and D2 before any predictive
claim. C1 is not required unless a session-dependent definition is adopted.

## 10. Group 5 — Liquidity and volume [SPEC §5.5]

Common to the group: priority **DATA_DEPENDENT** (except where noted); source
and semantics of volume must be explicit. MT5 `tick_volume` is broker tick
activity, not global traded volume [SPEC §4.5]; `real_volume` and book data are
only usable when the source provides them. Absent data → `UNAVAILABLE`, never
zero [SPEC §4.5]. Unlike venues are not combined without explicit
normalization and source labels. COMEX data stays a separate instrument
[SPEC §4.3].

| ID | Candidate | Spec text | Input & source | Notes |
|---|---|---|---|---|
| `liquidity.vwap` | Session VWAP and anchored VWAP | "with documented anchor rules" | Prices + a clearly identified volume source | Anchor rules, session definition **[TBD]** (C1); broker tick-volume VWAP must be labelled as such |
| `liquidity.volume_profile` | Volume profile / value area | "where true volume is available" | True volume only | Value-area rule **[TBD]**; `UNAVAILABLE` if only tick volume — or labelled tick-volume profile **[TBD]** |
| `liquidity.cvd` | Cumulative volume delta | "where aggressor-side data is trustworthy" | Aggressor-side trade data | Likely requires licensed futures data [SPEC §4.5] |
| `liquidity.spread` | Bid/ask spread percentile and spread shock detection | Named only | MT5 quotes/ticks/bar `spread_points` (broker) | Possible earliest studies in this group because the data is already ingested **[SUGGESTION]**; percentile window and shock rule **[TBD]**; stale/crossed quotes must be excluded per [SPEC §4.6] |
| `liquidity.book_imbalance` | Order-book imbalance and depth depletion | "only when book data is available and stable" | Broker depth or licensed depth | Missing feed → `UNAVAILABLE` |
| `liquidity.relative_volume` | Relative volume by session and time of day | Named only | Volume source must be labelled | Session/time-of-day buckets **[TBD]** |
| `liquidity.volume_price_divergence` | Volume/price divergence | "as a research feature" | Volume + price | Definition **[TBD]**; high spurious-signal risk, descriptive only |

Details: direction mostly `NOT_APPLICABLE` or **[TBD]**; warm-up **[TBD]**.
Data-quality dependencies: volume availability and meaning, stale/crossed
quotes, spread outliers, source outages [SPEC §4.6]. Leakage: session-anchored
values must not use not-yet-closed sessions; percentiles use past data only.
Tests: `UNAVAILABLE` when volume/book absent (never zero), source labelling,
tick-volume vs real-volume separation, crossed/stale quote rejection,
no-lookahead. Out-of-sample: §4.6. Prerequisites: core F1–F5 plus C2 (source-specific validation of
quote/tick, volume, book or external inputs, whose validated-input path or
adapter is **[TBD]** where it does not exist); C1 only for session-anchored studies
(`liquidity.vwap`, `liquidity.relative_volume`); `liquidity.spread` can use the
existing quote validation (`validate_quote`) and bar `spread_points`, but tick/quote
ingestion is not the historical-bar pipeline.

## 11. Group 6 — Macro and positioning [SPEC §5.6, §4.4]

Common to the group: priority **DATA_DEPENDENT**; type *contextual* — macro
signals are conditional context, not triggers, and the system must not assume a
constant inverse relationship between gold and dollar, yields, or risk proxies
[SPEC §5.6]. Direction: `NOT_APPLICABLE` unless a validated relationship is
documented **[SUGGESTION]**. Frequency is event/daily/weekly rather than bar
based. Every record carries publication time separately from observation period;
revised series use ALFRED vintages or another point-in-time archive
[SPEC §4.4]. Adapters are optional and separately testable [SPEC §4.4].
No adapter exists in the repository.

| ID | Candidate | Source named in spec | Specific constraints |
|---|---|---|---|
| `macro.dxy` | DXY return and trend state | Licensed or documented dollar-index provider | Provider **[TBD]** |
| `macro.us_yields` | US 10-year real yield and nominal yield changes | FRED/ALFRED, Treasury | Vintage handling required |
| `macro.yield_curve` | Yield-curve features, incl. 2Y/10Y changes "where useful" | FRED/ALFRED, Treasury | Features **[TBD]** |
| `macro.fed_funds_expectations` | Fed funds expectations | "only from time-stamped, licensed/documented data" | Source **[TBD]**; otherwise `UNAVAILABLE` |
| `macro.release_surprises` | CPI, PCE, PPI, NFP, unemployment surprises | BLS, BEA | Actual vs consensus "only if archived consensus values are available with timestamps"; otherwise `UNAVAILABLE` |
| `macro.fomc_event_risk` | FOMC event windows and event-risk classification | Federal Reserve calendars/statements | Window lengths and classes **[TBD]** |
| `macro.vix_risk` | VIX changes and risk regime | VIX or other documented risk proxy | Source **[TBD]** |
| `macro.cot_positioning` | COT net positioning, weekly changes, percentile/z-score, crowded-positioning measures | CFTC COT (disaggregated metals; correct COMEX gold contract) | Low-frequency context, never an intraday trigger; usually released Friday 3:30 p.m. Eastern for the prior Tuesday; availability by actual publication timestamp including holiday delays; percentile/z-score window and "crowded" rule **[TBD]** |
| `macro.etf_gold_context` | ETF flows and official gold-market context | ETF flows / World Gold Council data | "when frequency and history support it"; source licensing **[TBD]** |

Details: warm-up **[TBD]**; data-quality dependencies: source outages and
delayed publication [SPEC §4.6]; leakage risk (highest in this group): revised
data, consensus not archived, observation-vs-publication confusion, holiday
publication delays. Tests: a value published after `as_of_utc` is invisible;
vintage lookup returns the as-known value; COT release-time modelling incl.
holiday delay; missing consensus → `UNAVAILABLE`; alignment with bar
timestamps in UTC (time-zone conversion from Eastern). Out-of-sample: §4.6 and
stability of any relationship across periods. Prerequisites: core F1–F5, C2 (validated-input path/adapters,
point-in-time store **[TBD]**; none exist today); COMEX/futures data (if used) stays separate
[SPEC §4.3].

## 12. Priority summary

| Priority | Studies |
|---|---|
| FOUNDATION | F1–F5 core execution prerequisites (not studies); conditional C1/C2 and downstream D1/D2 are described in §5.1 |
| BASELINE | `trend.*` (7), `volatility.atr`, `volatility.realized`, `volatility.ewma`, `volatility.percentile_clustering`, `volatility.range_compression_bbw`, baseline regime components (assignment **[TBD]**) |
| STRUCTURE | `structure.*` (8) |
| DATA_DEPENDENT | `liquidity.*` (7; `liquidity.spread` can use existing quote data), `macro.*` (9) |
| ADVANCED | `volatility.garch11`, `volatility.egarch_gjr`, `volatility.har_rv`, `volatility.change_point`, `regime.hmm`, `regime.markov_switching`, `regime.bayesian_change_point`, `regime.clustering` |

The baseline regime components are listed under Group 4 and their class is
**[TBD]** (see §9). The catalogue contains 49 candidate entries across the six
groups (7 + 8 + 9 + 9 + 7 + 9). Recommended build order **[SUGGESTION]**:
FOUNDATION → BASELINE → STRUCTURE → DATA_DEPENDENT → ADVANCED.

## 13. Unresolved research/design decisions

1. Partly resolved (ADR-007, ADR-011): single-timeframe engine, independent multi-timeframe coordination, registry and configuration versioning exist (F1, F3). Open: scheduling.
2. Partly resolved (ADR-007): snapshot ID generation and structured provenance exist (F2). Open: Git/application revision tracking.
3. Session/market calendar and the policy for staleness during closures (C1, for session-dependent studies).
4. Partly resolved (ADR-007): `DataStatus` → `StudyStatus` mapping exists for caller-supplied `DataFailure`. The pipeline cutoff is bound to the runner cutoff (ADR-008). Open: automatic pipeline-to-runner failure wiring.
5. Warm-up lengths, parameters, and formulas for every study (none are approved; spec examples are illustrative).
6. Direction semantics for each trend/structure study.
7. Operational definitions and non-repainting rules for swings, BOS/CHoCH, zones, ranges, sessions, FVG, liquidity sweeps, and each candlestick pattern.
8. Representation of posterior probabilities/regime uncertainty given the current contract.
9. Regime label-assignment rules and the baseline-vs-structure priority of regime components.
10. Treatment of broker tick volume in VWAP/profile/relative-volume studies.
11. Choice of volatility-model library, fitting window, and refit schedule.
12. Macro/positioning providers, licensing, point-in-time store, and consensus archive.
13. Evaluation targets and metrics for each study (see [SPEC §8.2, §11]).
14. Correlation grouping method for correlated studies (D1).
15. Representation of non-bar macro/event/weekly study frequency in the required `StudyResult.timeframe` field. No sentinel value or new enum value is proposed here.
16. General (non-cache) study-result query API. The strict cache-oriented read `find_cached_study_result` already exists.
17. Validated-input paths for quotes/ticks (fail-closed ingestion) and for external macro, positioning, futures, volume and order-book data.
18. Exact broker gold symbol, to be discovered and validated through MT5.
