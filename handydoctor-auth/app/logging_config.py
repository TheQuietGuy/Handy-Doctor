"""
app/logging_config.py — Structured JSON logging for production.

Development:   human-readable coloured output (LOG_FORMAT=text, default)
Production:    one JSON object per line — parseable by Datadog, Loki, CloudWatch

Usage (in main.py lifespan):
    from app.logging_config import configure_logging
    configure_logging()

Each log record includes:
    timestamp   ISO-8601 with timezone
    level       DEBUG / INFO / WARNING / ERROR / CRITICAL
    logger      dotted logger name (e.g. "app.chat.ai.pipeline")
    message     the formatted log message
    request_id  injected by RequestIDMiddleware when present
    + any extra keyword args passed to the logger call
"""

from __future__ import annotations

import json
import logging
import logging.config
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any


# ── JSON formatter ────────────────────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    """
    Formats every log record as a single-line JSON object.
    Fields are ordered for readability in log viewers:
      timestamp → level → logger → message → [exception] → [extras]
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.getMessage(),
        }

        # Attach exception info when present
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        # Attach any extra fields (e.g. logger.info("x", extra={"user_id": uid}))
        _standard_attrs = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
        }
        for key, value in record.__dict__.items():
            if key not in _standard_attrs:
                try:
                    json.dumps(value)   # only include JSON-serialisable extras
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = str(value)

        return json.dumps(payload, ensure_ascii=False)


# ── Coloured text formatter (development) ────────────────────────────────────
_LEVEL_COLOURS = {
    "DEBUG":    "\033[0;36m",    # cyan
    "INFO":     "\033[0;32m",    # green
    "WARNING":  "\033[1;33m",    # bold yellow
    "ERROR":    "\033[0;31m",    # red
    "CRITICAL": "\033[1;31m",    # bold red
}
_RESET = "\033[0m"


class DevFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        colour = _LEVEL_COLOURS.get(record.levelname, "")
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        level = f"{colour}{record.levelname:<8}{_RESET}"
        logger_short = record.name.split(".")[-1]   # last component only
        msg = record.getMessage()
        line = f"{ts} {level} [{logger_short}] {msg}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


# ── Configuration ─────────────────────────────────────────────────────────────
def configure_logging(log_level: str | None = None) -> None:
    """
    Configure root logging for the application.

    Reads from environment variables:
        LOG_LEVEL   — DEBUG / INFO / WARNING / ERROR (default: INFO)
        LOG_FORMAT  — "json" or "text" (default: "text" in dev, "json" in prod)

    Call once at application startup, before any log messages are emitted.
    """
    level_name = (log_level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    # Auto-detect format: use JSON when running in a container / production
    default_format = "json" if _is_production() else "text"
    log_format = os.environ.get("LOG_FORMAT", default_format).lower()

    formatter: logging.Formatter
    if log_format == "json":
        formatter = JSONFormatter()
    else:
        formatter = DevFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # Quieten noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("passlib").setLevel(logging.ERROR)
    logging.getLogger("apscheduler").setLevel(logging.INFO)

    if log_format == "json":
        # Suppress uvicorn's default access logger in favour of our middleware
        logging.getLogger("uvicorn.access").propagate = False

    logging.getLogger(__name__).info(
        "Logging configured",
        extra={"log_level": level_name, "log_format": log_format},
    )


def _is_production() -> bool:
    """Heuristic: running in a container or with an explicit ENV variable."""
    env = os.environ.get("ENV", os.environ.get("ENVIRONMENT", "")).lower()
    if env in ("production", "prod", "staging"):
        return True
    # Docker / K8s sets HOSTNAME to the container ID
    hostname = os.environ.get("HOSTNAME", "")
    if hostname and len(hostname) == 12 and hostname.isalnum():
        return True
    return False


# ── Request-ID context middleware ─────────────────────────────────────────────
import contextvars

_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


class RequestIDFilter(logging.Filter):
    """Injects the current request_id into every log record in this request context."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get(None)
        return True


def get_request_id() -> str | None:
    return _request_id_var.get(None)


def set_request_id(request_id: str) -> None:
    _request_id_var.set(request_id)
