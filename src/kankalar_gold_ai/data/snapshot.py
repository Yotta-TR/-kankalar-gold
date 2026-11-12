"""Deterministic OHLC bar snapshot identity.

The ``bars-v1`` payload is canonical JSON encoded as UTF-8 with sorted keys,
compact separators, ``ensure_ascii=False`` and ``allow_nan=False``. Timestamps
are normalized to UTC ISO-8601 with a trailing ``Z``. Price and volume numbers
are converted to finite floats; optional values remain JSON ``null`` and integer
fields remain integers. The top-level object contains ``algorithm``, ``source``,
``instrument``, ``timeframe`` and ``bars``. Each ordered bar object contains
``timestamp_utc``, ``open``, ``high``, ``low``, ``close``, ``volume``,
``tick_volume``, ``real_volume`` and ``spread_points``. Python's JSON float
encoding is used after float normalization, so ``-0.0`` remains distinct from
``0.0``. Retrieval timestamps are deliberately excluded.
"""

import hashlib
import json
from datetime import timezone

from kankalar_gold_ai.core.contracts import MarketBar, as_utc
from kankalar_gold_ai.data.bar_validation import bar_integrity_reasons


def canonical_bar_snapshot_json(bars: tuple[MarketBar, ...]) -> str:
    """Return the exact canonical JSON representation used by the snapshot hash."""
    if not isinstance(bars, tuple):
        raise TypeError("bars must be an ordered tuple")
    if not bars:
        raise ValueError("bars must not be empty")
    timeframe = bars[0].timeframe
    reasons = bar_integrity_reasons(bars, timeframe)
    if reasons:
        raise ValueError(f"bar snapshot is inconsistent: {', '.join(reasons)}")

    first = bars[0]
    payload = {
        "algorithm": "bars-v1",
        "bars": [
            {
                "close": float(bar.close),
                "high": float(bar.high),
                "low": float(bar.low),
                "open": float(bar.open),
                "real_volume": bar.real_volume,
                "spread_points": bar.spread_points,
                "tick_volume": bar.tick_volume,
                "timestamp_utc": as_utc(bar.timestamp_utc)
                .astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "volume": None if bar.volume is None else float(bar.volume),
            }
            for bar in bars
        ],
        "instrument": first.instrument,
        "source": first.source,
        "timeframe": first.timeframe,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def compute_bar_snapshot_id(bars: tuple[MarketBar, ...]) -> str:
    """Compute a versioned SHA-256 identity over every relevant ordered bar field."""
    canonical = canonical_bar_snapshot_json(bars).encode("utf-8")
    return f"bars-v1:sha256:{hashlib.sha256(canonical).hexdigest()}"
