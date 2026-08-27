"""Runtime configuration, read from the environment.

Lambda gets these from the CDK stack. Scripts get them from ``.env.local`` or
from the deployed stack outputs. Nothing here is a secret.
"""

from __future__ import annotations

import os
from functools import lru_cache

PROJECT = "adventure-agent"
REGION = "us-east-1"

# --- safety limits -------------------------------------------------------
# These are product safety requirements. They live here as constants so they
# are enforced in code and testable, never left to a model to decide.

QUIET_HOURS_START = 22  # inclusive, player-local
QUIET_HOURS_END = 8  # exclusive, player-local
MAX_STORY_MESSAGES_PER_DAY = 6
MAX_CALLS_PER_ARC = 1

# How often the out-of-character footer is appended to in-character
# messages. The spec accepts a persistent footer OR an always-available
# /real command; we ship both, with the footer on the first story message
# of each player-local day so it stays visible without becoming wallpaper.
# "always" | "daily" | "never"
FOOTER_MODE = "daily"

# Typing-indicator delay before an in-character reply. Instant replies break
# the illusion; long ones feel broken. Scaled by message length, clamped here.
MIN_REPLY_DELAY_S = 1.2
MAX_REPLY_DELAY_S = 4.0

# How long a Lambda container may reuse a cached secret before re-fetching.
SECRET_CACHE_TTL_S = 300


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


@lru_cache(maxsize=1)
def table_name() -> str:
    return env("ADVENTURE_TABLE_NAME")


@lru_cache(maxsize=1)
def secret_name() -> str:
    return env("ADVENTURE_SECRET_NAME")


@lru_cache(maxsize=1)
def env_name() -> str:
    return env("ADVENTURE_ENV", "dev")
