"""Structured JSON logging.

One line of JSON per event so CloudWatch Logs Insights can filter on
``player_id``, ``event`` and ``latency_ms`` without regex archaeology. Named
``logs`` rather than ``logging`` so it never shadows the stdlib module.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import Any

_LEVEL = os.environ.get("ADVENTURE_LOG_LEVEL", "INFO").upper()

_logger = logging.getLogger("adventure")
if not _logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.propagate = False
_logger.setLevel(_LEVEL)

# Values that must never reach a log line, whatever a caller passes in.
_REDACT_KEYS = {
    "token",
    "bot_token",
    "telegram_bot_token",
    "auth_token",
    "secret",
    "webhook_secret",
    "telegram_webhook_secret",
    "password",
    "authorization",
    # GPS is never persisted; it must not survive in logs either.
    "latitude",
    "longitude",
    "lat",
    "lon",
}


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if k.lower() in _REDACT_KEYS else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def log(event: str, level: int = logging.INFO, **fields: Any) -> None:
    payload = {"event": event, **_scrub(fields)}
    _logger.log(level, json.dumps(payload, default=str, ensure_ascii=False))


def info(event: str, **fields: Any) -> None:
    log(event, logging.INFO, **fields)


def warn(event: str, **fields: Any) -> None:
    log(event, logging.WARNING, **fields)


def error(event: str, **fields: Any) -> None:
    log(event, logging.ERROR, **fields)


@contextmanager
def timed(event: str, **fields: Any):
    """Log ``event`` on exit with a ``latency_ms`` field.

    Cost and latency visibility is a pilot deliverable, so every outbound call
    is wrapped in this.
    """
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except Exception as exc:
        outcome = "error"
        fields["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        log(
            event,
            logging.ERROR if outcome == "error" else logging.INFO,
            outcome=outcome,
            latency_ms=round(elapsed, 1),
            **fields,
        )
