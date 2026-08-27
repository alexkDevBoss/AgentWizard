"""The single outbound chokepoint.

**Nothing else in this codebase may call a channel's send method directly.**
Every outbound message goes through :func:`send_to_player`, because that is
where the safety gates live: player status, quiet hours, the daily rate limit,
the content-validation pass, and the out-of-character footer.

Gate order is deliberate:

1. **status**   -- a stopped or paused player never receives story content
2. **quiet hours** -- checked before anything is spent, so a deferred beat
   keeps its allowance
3. **validation** -- a message that fails is replaced, not sent as-is
4. **rate limit**  -- claimed last, so only a message that will actually go out
   burns one of the player's six

System replies (STOP confirmations, ``/real``, PAUSE acknowledgements) skip
gates 2-4 by design; see :class:`~backend.core.models.MessageKind`.
"""

from __future__ import annotations

import random
import time

from backend.channels.telegram import TelegramClient
from backend.core import config, logs, safety, secrets, store
from backend.core.clock import in_quiet_hours, iso, next_allowed_time, now_utc
from backend.core.models import (
    BlockReason,
    Channel,
    Direction,
    EventKind,
    MessageKind,
    Player,
    PlayerStatus,
    SendResult,
)
from backend.core.store import RateLimited
from backend.core.validation import validate_outbound

_client: TelegramClient | None = None


def telegram() -> TelegramClient:
    global _client
    if _client is None:
        _client = TelegramClient(secrets.telegram_bot_token())
    return _client


def reset_client() -> None:
    """Test hook."""
    global _client
    _client = None


def _reply_delay(text: str) -> float:
    """A short, randomised, length-aware pause. Instant replies break the illusion."""
    typing_time = len(text) / 45.0
    return min(
        config.MAX_REPLY_DELAY_S,
        max(config.MIN_REPLY_DELAY_S, typing_time * random.uniform(0.7, 1.3)),
    )


def _with_footer(text: str, *, story_messages_today: int) -> str:
    mode = config.FOOTER_MODE
    if mode == "never":
        return text
    if mode == "always" or story_messages_today == 1:
        return f"{text}\n\n{safety.FOOTER}"
    return text


def send_to_player(
    player: Player,
    text: str,
    *,
    kind: MessageKind = MessageKind.STORY,
    channel: Channel = Channel.TELEGRAM,
    beat_id: str | None = None,
    source: str = "engine",
    simulate_typing: bool = True,
) -> SendResult:
    """Attempt one outbound message. Returns why it did not go out, if it did not.

    A blocked result is a normal outcome, not an error. Callers reschedule on
    ``QUIET_HOURS`` (using ``retry_at``) and drop on ``PLAYER_STOPPED``.
    """
    now = now_utc()
    is_story = kind is MessageKind.STORY

    # --- gate 1: player status ------------------------------------------
    if is_story and player.status is not PlayerStatus.ACTIVE:
        reason = (
            BlockReason.PLAYER_STOPPED
            if player.status is PlayerStatus.STOPPED
            else BlockReason.PLAYER_PAUSED
        )
        return _blocked(player, text, reason, channel, beat_id, source)

    if channel is Channel.TELEGRAM and player.telegram_chat_id is None:
        return _blocked(player, text, BlockReason.NO_CHANNEL, channel, beat_id, source)

    # --- gate 2: quiet hours --------------------------------------------
    if is_story and in_quiet_hours(now, player.timezone):
        retry_at = iso(next_allowed_time(now, player.timezone))
        return _blocked(
            player,
            text,
            BlockReason.QUIET_HOURS,
            channel,
            beat_id,
            source,
            retry_at=retry_at,
        )

    # --- gate 3: content validation --------------------------------------
    outgoing = text
    substitution: BlockReason | None = None
    if is_story:
        result = validate_outbound(text)
        if not result.ok:
            # Do not send; log, flag for the operator, and fall back to a safe
            # pre-written line -- the story still moves.
            logs.warn(
                "validation.failed",
                player_id=player.player_id,
                rules=result.rule_ids,
                details=[v.detail for v in result.violations],
                beat_id=beat_id,
                source=source,
            )
            store.record_event(
                player.player_id,
                direction=Direction.OUT,
                channel=channel,
                kind=EventKind.BLOCKED,
                text=text,
                reason=str(BlockReason.VALIDATION_FAILED),
                rules=result.rule_ids,
                needs_review=True,
                beat_id=beat_id,
                source=source,
            )
            outgoing = safety.SAFE_FALLBACK
            substitution = BlockReason.VALIDATION_FAILED

    # --- gate 4: daily rate limit ----------------------------------------
    used_today = 0
    if is_story:
        try:
            used_today = store.consume_daily_quota(
                player.player_id,
                player.timezone,
                limit=config.MAX_STORY_MESSAGES_PER_DAY,
            )
        except RateLimited:
            return _blocked(
                player, outgoing, BlockReason.RATE_LIMITED, channel, beat_id, source
            )
        outgoing = _with_footer(outgoing, story_messages_today=used_today)

    # --- send -------------------------------------------------------------
    client = telegram()
    if is_story and simulate_typing:
        try:
            client.send_chat_action(player.telegram_chat_id, "typing")
        except Exception as exc:  # a failed typing indicator must never block a beat
            logs.warn(
                "telegram.typing_failed", player_id=player.player_id, error=str(exc)
            )
        time.sleep(_reply_delay(outgoing))

    sent = client.send_message(player.telegram_chat_id, outgoing)

    store.record_event(
        player.player_id,
        direction=Direction.OUT,
        channel=channel,
        kind=EventKind.SYSTEM
        if kind is MessageKind.SYSTEM_REPLY
        else EventKind.MESSAGE,
        text=outgoing,
        message_kind=str(kind),
        telegram_message_id=sent.get("message_id"),
        beat_id=beat_id,
        source=source,
        substituted=bool(substitution),
        quota_used=used_today or None,
    )
    logs.info(
        "message.sent",
        player_id=player.player_id,
        kind=str(kind),
        channel=str(channel),
        chars=len(outgoing),
        quota_used=used_today or None,
        substituted=bool(substitution),
        source=source,
    )
    return SendResult(
        sent=True,
        reason=substitution,
        message_id=str(sent.get("message_id")),
        text=outgoing,
    )


def send_system_to_chat(chat_id: int | str, text: str) -> None:
    """Reply to a chat that has no player behind it.

    The one sanctioned way to reach a channel without going through the gates,
    because there is no player to gate on: an unenrolled chat, or a bad
    enrolment code. Only ever carries fixed out-of-character copy from
    :mod:`backend.core.safety` -- never generated text.
    """
    try:
        telegram().send_message(chat_id, text)
        logs.info("message.sent_unenrolled", chat_id=chat_id, chars=len(text))
    except Exception as exc:
        logs.error("message.unenrolled_failed", chat_id=chat_id, error=str(exc))


def _blocked(
    player: Player,
    text: str,
    reason: BlockReason,
    channel: Channel,
    beat_id: str | None,
    source: str,
    *,
    retry_at: str | None = None,
) -> SendResult:
    logs.info(
        "message.blocked",
        player_id=player.player_id,
        reason=str(reason),
        retry_at=retry_at,
        beat_id=beat_id,
        source=source,
    )
    store.record_event(
        player.player_id,
        direction=Direction.OUT,
        channel=channel,
        kind=EventKind.BLOCKED,
        text=text,
        reason=str(reason),
        retry_at=retry_at,
        beat_id=beat_id,
        source=source,
    )
    return SendResult(sent=False, reason=reason, retry_at=retry_at, text=text)
