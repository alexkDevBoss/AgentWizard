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

# --- story generation ----------------------------------------------------
# Claude on Bedrock, reached with the Anthropic SDK's `AnthropicBedrock`
# client. The `us.` prefix is a cross-region inference profile, not a region
# override -- the bare `anthropic.claude-*` ids are rejected for on-demand
# throughput, and this account is not entitled to the newer ids the Bedrock
# Messages ("Mantle") endpoint serves. Both facts are verified, not assumed;
# see the README before changing this string.
STORY_MODEL = "us.anthropic.claude-opus-4-6-v1"

#: The safety reviewer. Same model on purpose: a weaker reviewer is worse than
#: no second opinion, because it reads as coverage while missing what the
#: regex layer already cannot see.
REVIEW_MODEL = STORY_MODEL

# A story message is capped at 1200 characters by validation, so generation
# never needs a large budget. Kept low because the whole call has to fit
# inside API Gateway's 30s ceiling.
STORY_MAX_TOKENS = 2000
REVIEW_MAX_TOKENS = 1000

# Effort trades thinking depth against latency. `medium` for writing (it has
# to hold voice, arc and safety at once) and `low` for the review pass, which
# is a judgement against an explicit list.
STORY_EFFORT = "medium"
REVIEW_EFFORT = "low"

# Per-call ceilings, sized against a hard external limit: API Gateway gives
# the webhook 30 seconds and then returns a 504, and Telegram retries anything
# that is not a 2xx. Both model calls, a Bot API round trip and the table
# writes have to fit inside that with margin.
#
# Measured against the live account: writing takes 5.6-10.4s, reviewing ~2s.
# The worst case below is 13 + 5 + 5 = 23s of model time, leaving ~6s of head
# room. Writing gets no retry because a second attempt would not fit; the
# review does, because it is cheap and its budget is small.
WRITE_TIMEOUT_S = 13.0
WRITE_RETRIES = 0
REVIEW_TIMEOUT_S = 5.0
REVIEW_RETRIES = 1

# How much of the player's timeline is replayed to the model as conversation.
# Enough for continuity, bounded so a long arc cannot grow the prompt without
# limit.
CONTEXT_EVENT_LIMIT = 40

#: The arc every new player is enrolled onto, unless the operator picks
#: another. Matches a file in backend/story/arcs/.
DEFAULT_ARC_ID = "nightjar"


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
