# GitHub Copilot — Initial Project Bootstrap Prompt

You are the senior software architect for **KANKALAR GOLD AI**. Read `KANKALAR_GOLD_AI_MASTER_SPEC_v0.1.md` completely before creating or editing code. The specification is the source of truth.

## Immediate task: Phase 0 only

Do not implement indicators, strategies, machine learning, MT5 connectivity, Telegram delivery, or any trading logic yet.

Create a clean, testable Python project skeleton for Phase 0:

1. Inspect the current repository before changing anything. Do not overwrite unrelated files.
2. Create `pyproject.toml`, package skeleton under `src/kankalar_gold_ai/`, `tests/`, `config/`, and the core documentation listed in the master specification.
3. Define typed interfaces for market data, study results, strategy proposals, risk decisions, signal lifecycle events, and persistence. Keep implementations as safe stubs where later phases will fill them.
4. Add validated research-only configuration. Defaults must set `safe_mode=true`, `allow_live_trading=false`, Telegram disabled, and virtual risk mode.
5. Add logging with credential redaction and UTC timestamps.
6. Add a mock market-data provider and small deterministic test fixtures. Unit tests must not use live network calls.
7. Add an architectural safety test that scans the application package and fails if `mt5.order_send`, an equivalent order-placement function, or an automatic-trading route is introduced. No order-execution module should exist.
8. Add tests for configuration validation, UTC timestamp handling, study status enums, and the common study-result schema.
9. Write `README.md` with setup, test, safety boundary, and the next development phase.
10. Run the configured tests, formatter, linter, type checker, and `git diff --check` if configured. Report exact command outputs and any unavailable checks.

## Strict rules

- Do not claim a test passed unless you actually ran it and observed the result.
- Do not access broker credentials, live accounts, or send trading orders.
- Do not connect to MT5 or external APIs in Phase 0.
- Do not fabricate sample prices or performance results. Clearly label synthetic test fixtures.
- Do not optimise or invent architecture beyond the master specification without documenting the trade-off.
- Do not edit `KANKALAR LOSS AI`, its data, processes, tokens, or database. This is a separate project.
- Make a concise plan first, then implement Phase 0 only.
- At the end, report: changed files, architecture created, tests run with exact counts, safety-test result, known limitations, and the proposed Phase 1 plan. Stop and wait for review.
