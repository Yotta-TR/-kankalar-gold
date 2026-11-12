"""UTC logging with recursive redaction of common credential fields."""

import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any

_SENSITIVE_SUFFIXES = (
    "apikey",
    "accesstoken",
    "refreshtoken",
    "bottoken",
    "authorization",
    "password",
    "passwd",
    "secret",
    "credential",
    "credentials",
)
_FIELD_ASSIGNMENT = re.compile(
    r"""(?P<prefix>["']?(?P<key>[A-Za-z0-9_.-]*"""
    r"""(?:api[_-]?key|access[_-]?token|refresh[_-]?token|bot[_-]?token|"""
    r"""authorization|password|passwd|secret|credentials?)"""
    r"""(?:[_-][A-Za-z0-9_.-]+)*)["']?\s*[:=]\s*)"""
    r"""(?P<value>"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|[^\s,;}]+)""",
    re.IGNORECASE,
)
_BEARER_CREDENTIAL = re.compile(r"(?i)\bBearer\s+[^\s,;\"']+")
_LOG_RECORD_FIELDS = frozenset(
    logging.LogRecord(
        name="",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
)


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return any(suffix in normalized for suffix in _SENSITIVE_SUFFIXES)


def _redact_text(value: str) -> tuple[str, bool]:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        cleaned, changed = _redact_value(parsed)
        if changed:
            return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")), True

    def replace_assignment(match: re.Match[str]) -> str:
        if not _is_sensitive_key(match.group("key")):
            return match.group(0)
        original = match.group("value")
        quote = original[0] if original.startswith(("'", '"')) else ""
        replacement = f"{quote}[REDACTED]{quote}" if quote else "[REDACTED]"
        return f"{match.group('prefix')}{replacement}"

    redacted = _FIELD_ASSIGNMENT.sub(replace_assignment, value)
    redacted = _BEARER_CREDENTIAL.sub("Bearer [REDACTED]", redacted)
    return redacted, redacted != value


def _redact_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        cleaned: dict[Any, Any] = {}
        changed = False
        for key, item in value.items():
            if _is_sensitive_key(key):
                cleaned[key] = "[REDACTED]"
                changed = True
            else:
                cleaned[key], item_changed = _redact_value(item)
                changed = changed or item_changed
        return cleaned, changed
    if isinstance(value, list):
        cleaned_list: list[Any] = []
        changed = False
        for item in value:
            cleaned, item_changed = _redact_value(item)
            cleaned_list.append(cleaned)
            changed = changed or item_changed
        return cleaned_list, changed
    if isinstance(value, tuple):
        cleaned_items = [_redact_value(item) for item in value]
        return (
            tuple(item for item, _ in cleaned_items),
            any(changed for _, changed in cleaned_items),
        )
    return value, False


class CredentialRedactionFilter(logging.Filter):
    """Redact structured fields, interpolated messages, and exception traces."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.msg, str):
            record.msg, _ = _redact_value(record.msg)
        record.args, _ = _redact_value(record.args)
        for key, value in tuple(record.__dict__.items()):
            if key in _LOG_RECORD_FIELDS or key in {"message", "asctime"}:
                continue
            if _is_sensitive_key(key):
                record.__dict__[key] = "[REDACTED]"
            else:
                record.__dict__[key], _ = _redact_value(value)

        record.msg = _redact_text(record.getMessage())[0]
        record.args = ()
        if record.exc_info:
            record.exc_text = _redact_text(logging.Formatter().formatException(record.exc_info))[0]
            record.exc_info = None
        if record.exc_text:
            record.exc_text = _redact_text(record.exc_text)[0]
        if record.stack_info:
            record.stack_info = _redact_text(record.stack_info)[0]
        return True


class UtcFormatter(logging.Formatter):
    def converter(self, timestamp: float | None = None) -> time.struct_time:
        return time.gmtime(timestamp)


def configure_logging(
    level: int = logging.INFO,
    handler: logging.Handler | None = None,
) -> logging.Logger:
    """Configure the application logger; callers may inject a handler in tests."""
    logger = logging.getLogger("kankalar_gold_ai")
    logger.setLevel(level)
    logger.propagate = False
    if handler is None:
        handler = logging.StreamHandler()
    handler.setFormatter(UtcFormatter("%(asctime)sZ %(levelname)s %(name)s %(message)s"))
    handler.addFilter(CredentialRedactionFilter())
    logger.handlers.clear()
    logger.addHandler(handler)
    return logger


def log_exception(logger: logging.Logger, message: str, error: BaseException) -> None:
    """Log an exception while allowing the configured filter to redact its trace."""
    logger.error("%s: %s", message, error, exc_info=error)
