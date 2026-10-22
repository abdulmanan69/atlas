"""Structured JSON logging with correlation IDs and secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_id_ctx: ContextVar[str | None] = ContextVar("tenant_id", default=None)
# Set when the request authenticated with a tenant API key instead of a user session,
# so logs and audit entries can attribute the action to the key.
api_key_id_ctx: ContextVar[str | None] = ContextVar("api_key_id", default=None)

_SENSITIVE_KEYS = re.compile(
    r"(authorization|password|secret|token|api[_-]?key|cookie|signature)", re.IGNORECASE
)
_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+")
# The AI gateway's live speech-to-text address carries a secret in its path (ADR 0007).
_LIVE_STT_PATH = re.compile(r"(/live-stt/)[^/\s\"'?#]+")

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
}


def redact(value: Any) -> Any:
    """Recursively mask values whose keys look sensitive."""
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if _SENSITIVE_KEYS.search(str(k)) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def scrub_text(text: str) -> str:
    """Mask bearer tokens and URL path secrets in free text."""
    return _LIVE_STT_PATH.sub(r"\1[REDACTED]", _BEARER.sub("Bearer [REDACTED]", text))


class _UrlSecretFilter(logging.Filter):
    """httpx logs every request's URL at INFO; mask path secrets before any handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and any("/live-stt/" in str(a) for a in record.args):
            record.args = tuple(
                scrub_text(str(a)) if "/live-stt/" in str(a) else a for a in record.args
            )
        return True


logging.getLogger("httpx").addFilter(_UrlSecretFilter())


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": scrub_text(record.getMessage()),
        }
        if rid := request_id_ctx.get():
            payload["request_id"] = rid
        if tid := tenant_id_ctx.get():
            payload["tenant_id"] = tid
        if kid := api_key_id_ctx.get():
            payload["api_key_id"] = kid
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if extras:
            payload.update(redact(extras))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str, service: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)
