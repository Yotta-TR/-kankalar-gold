# Data Dictionary

Phase 0 defines contracts, not production datasets. Synthetic values in
`tests/fixtures/` are deterministic test-only values and are never market data.

| Record | Required fields | Invariant |
|---|---|---|
| Market bar | instrument, timeframe, UTC timestamp, OHLC, optional volume, source | OHLC is internally consistent; source and units remain explicit |
| Study result | study id/version, instrument/timeframe, UTC as-of, status, direction, inputs, evidence, limitations | Missing values remain missing; status is one of the common contract values |
| Strategy proposal | proposal/strategy ids, instrument, direction, creation/expiry, trigger, invalidation, evidence | A proposal is not an executed trade |
| Risk decision | proposal id, approved/rejected outcome, reason codes, mode | Phase 0 mode is virtual |
| Signal lifecycle event | event/signal ids, state, UTC occurrence time, reason code | Events are append-oriented and auditable |

Store all timestamps as timezone-aware UTC. Future adapters must retain source
timestamps, retrieval timestamps, units, quality state, and snapshot/version
references rather than silently normalizing away provenance.
