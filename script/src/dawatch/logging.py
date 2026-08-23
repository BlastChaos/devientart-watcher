"""Structured logging.

structlog wraps the standard library, so httpx's own loggers are captured by
the same configuration rather than bypassing it.
"""

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from dawatch.config import Settings

SENSITIVE_KEYS = frozenset(
    {"access_token", "client_secret", "client_id", "authorization", "token", "password"}
)

REDACTED = "***REDACTED***"


def redact_secrets(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace the value of any sensitive-looking key with a placeholder.

    Matching is case-insensitive and substring-based, so ``DA_CLIENT_SECRET``
    and ``Authorization`` are both caught. This is the last line of defence:
    values should not reach a log call in the first place.
    """
    for key in list(event_dict):
        lowered = key.lower()
        if any(sensitive in lowered for sensitive in SENSITIVE_KEYS):
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the stdlib root logger for this process."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer()
        if settings.env == "dev"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
