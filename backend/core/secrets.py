"""Cached access to the environment's Secrets Manager entry.

One JSON blob per environment. Cached in the Lambda container for
``SECRET_CACHE_TTL_S`` so a warm container does not pay a Secrets Manager call
per message, but a rotated credential still takes effect within minutes.
"""

from __future__ import annotations

import json
import os
import time

import boto3

from backend.core import config

_cache: dict | None = None
_fetched_at: float = 0.0
_client = None


def _sm():
    global _client
    if _client is None:
        _client = boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", config.REGION),
        )
    return _client


def all_secrets(*, force: bool = False) -> dict:
    global _cache, _fetched_at
    fresh = (
        _cache is not None and (time.time() - _fetched_at) < config.SECRET_CACHE_TTL_S
    )
    if fresh and not force:
        return _cache

    raw = _sm().get_secret_value(SecretId=config.secret_name())["SecretString"]
    _cache = json.loads(raw)
    _fetched_at = time.time()
    return _cache


def get(key: str, *, required: bool = True) -> str:
    value = (all_secrets().get(key) or "").strip()
    if not value and required:
        raise RuntimeError(
            f"secret key {key!r} is empty in {config.secret_name()!r} -- "
            "fill it in with `aws secretsmanager put-secret-value`"
        )
    return value


def telegram_bot_token() -> str:
    return get("telegram_bot_token")


def telegram_webhook_secret() -> str:
    return get("telegram_webhook_secret")


def reset_cache() -> None:
    """Test hook."""
    global _cache, _fetched_at
    _cache = None
    _fetched_at = 0.0
