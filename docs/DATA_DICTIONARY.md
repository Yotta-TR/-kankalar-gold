# Data Dictionary

Phase 0 defines contracts, not production datasets. Synthetic values in
`tests/fixtures/` are deterministic test-only values and are never market data.

| Record | Required fields | Invariant |
|---|---|---|
| Market bar | instrument, timeframe, UTC timestamp, OHLC, optional volume, source | OHLC is internally consistent; source and units remain explicit |
| Market tick | instrument, UTC timestamp (optionally milliseconds), bid/ask/last, volume, flags, source, retrieval timestamp | Price/time/flag consistency is validated by the read-only adapter; exact repeated events are deduplicated by event identity, but distinct same-time updates are retained |
| Market quote | instrument, UTC timestamp (optionally milliseconds), bid/ask/last, source, retrieval timestamp | Crossed/non-positive quotes are invalid; explicit freshness and maximum-spread limits are applied by `validate_quote` |
| Study result | study id/version, instrument/timeframe, UTC as-of, status, direction, inputs, evidence, limitations | Missing values remain missing; status is one of the common contract values |
| Strategy proposal | proposal/strategy ids, instrument, direction, creation/expiry, trigger, invalidation, evidence | A proposal is not an executed trade |
| Risk decision | proposal id, approved/rejected outcome, reason codes, mode | Phase 0 mode is virtual |
| Signal lifecycle event | event/signal ids, state, UTC occurrence time, reason code | Events are append-oriented and auditable |

Store all timestamps as timezone-aware UTC. Future adapters must retain source
timestamps, retrieval timestamps, units, quality state, and snapshot/version
references rather than silently normalizing away provenance.

SQLite schema v4 hashes quote/tick identity without retrieval time. This
preserves distinct same-timestamp updates while rejecting an exact repeat from
the same source; event rows remain append-oriented through the persistence API.
