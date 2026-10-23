"""Secret redaction applied to every log line and audit payload before it is persisted."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Keys whose values are always redacted, matched case-insensitively as substrings.
SENSITIVE_KEYS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "cookie",
    "session",
    "authorization",
    "bearer",
)

# Value patterns that look like credentials regardless of key name.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),  # sk- style API keys (Anthropic/OpenAI)
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
)


def redact_text(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEYS)


def redact(value: Any) -> Any:
    """Recursively redact a JSON-like structure."""
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and _is_sensitive_key(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
