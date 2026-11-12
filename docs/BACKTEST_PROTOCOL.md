# Backtest Protocol

Backtests are research evidence, not performance promises. Before evaluation,
freeze data, strategy definitions, costs, and the chronological split plan.

1. Validate source, timestamps, completeness, and point-in-time availability.
2. Keep training, validation, untouched holdout, and later paper periods ordered
   in time. Fit transforms and calibrators only on permitted earlier periods.
3. Use bid/ask-aware entries/exits, variable spread, commission, slippage,
   session constraints, gaps, and conservative stop/target sequencing where
   history permits. Label lower-fidelity simulations clearly.
4. Report gross and net results, trade count, uncertainty, drawdown, expectancy,
   risk-adjusted and calibration metrics, and cost sensitivity.
5. Do not promote on a single metric, a few outliers, or training performance.
   Record limitations and require review before a paper-trading trial.

Phase 0 contains no backtester and no performance claims.
