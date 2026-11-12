"""Static safety policies for keeping the application research-only."""

from kankalar_gold_ai.security.trading_safety import (
    READ_ONLY_MT5_APIS,
    SafetyViolation,
    analyze_source,
    scan_package,
)

__all__ = ["READ_ONLY_MT5_APIS", "SafetyViolation", "analyze_source", "scan_package"]
