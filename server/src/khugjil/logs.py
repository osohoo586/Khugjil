"""Structured logging with an allowlist redaction filter.

Only known, non-sensitive keys survive. Event names must be constant identifiers, so a value can
never be smuggled into a log line through an f-string. Exceptions are reduced to their type and
code locations: messages and locals can carry email content and are never logged.
Third-party (stdlib) loggers are reduced to logger name + level for the same reason.
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog

ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "event",
        "level",
        "timestamp",
        "logger",
        "tenant_id",
        "reason_code",
        "msgid_sha256",
        "request_id",
        "route",
        "method",
        "status",
        "duration_ms",
        "transaction_id",
        "delivery_id",
        "device_id",
        "member_id",
        "channel",
        "job",
        "attempt",
        "error_code",
        "count",
        "peer_ip",
        "smtp_code",
        "exc_type",
        "exc_frames",
        "transport",
        # Shape of an unrecognised Gmail forwarding confirmation: MIME types and digit-run
        # lengths only (never text), to adapt the recogniser (ingest/forwarding.py).
        "fwd_shape",
        "fwd_digits",
        # DKIM d= domains of an allowed bank sender's email that failed check c (no content).
        "dkim_domains",
    }
)
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_SAFE_VALUE_TYPES = (str, int, float, bool, type(None))


def _scrub_exception(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    exc_info = event_dict.pop("exc_info", None)
    event_dict.pop("exception", None)
    event_dict.pop("stack", None)
    if exc_info:
        if exc_info is True:
            exc_info = sys.exc_info()
        if isinstance(exc_info, BaseException):
            exc: BaseException | None = exc_info
        elif isinstance(exc_info, tuple):
            exc = exc_info[1]
        else:
            exc = None
        if exc is not None:
            event_dict["exc_type"] = type(exc).__name__
            frames = traceback.extract_tb(exc.__traceback__)[-8:]
            event_dict["exc_frames"] = [
                f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}:{f.name}" for f in frames
            ]
    return event_dict


def _allowlist(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    event = event_dict.get("event")
    if not isinstance(event, str) or not _EVENT_RE.fullmatch(event):
        event_dict["event"] = "invalid_event_name"
    out: dict[str, Any] = {}
    for key, value in event_dict.items():
        if key not in ALLOWED_KEYS:
            continue
        if (key == "exc_frames" and isinstance(value, list)) or isinstance(
            value, _SAFE_VALUE_TYPES
        ):
            out[key] = value
        else:
            out[key] = str(value) if key.endswith("_id") else "<redacted>"
    return out


class _StdlibBridge(logging.Handler):
    """Forward third-party log records without their (possibly sensitive) message."""

    def emit(self, record: logging.LogRecord) -> None:
        kwargs: dict[str, Any] = {"logger": record.name}
        if record.exc_info and record.exc_info[1] is not None:
            kwargs["exc_info"] = record.exc_info[1]
        structlog.get_logger().log(record.levelno, "stdlib_log", **kwargs)


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    numeric = logging.getLevelName(level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _scrub_exception,
            _allowlist,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,
    )
    root = logging.getLogger()
    root.handlers = [_StdlibBridge()]
    root.setLevel(numeric)
    # aiosmtpd logs SMTP commands (including RCPT addresses) at INFO/DEBUG.
    for noisy in ("mail.log", "uvicorn.access", "arq.worker", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(**initial: Any) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(**initial)
    return logger
