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
