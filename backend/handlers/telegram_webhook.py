"""Telegram webhook Lambda.

Thin by design: verify, resolve the player, delegate. All policy lives in
``backend/core``.

This handler always answers 200 once the request is authenticated, even when
handling fails. Telegram retries non-2xx responses aggressively, and a retry
storm against a broken handler is worse than one dropped update -- the failure
is logged at ERROR and shows up in the admin timeline.
"""

from __future__ import annotations

import hmac
import json
from typing import Any

from backend.channels import telegram as tg
from backend.core import (
    config,
    control,
    dispatch,
    engine,
    logs,
    safety,
    secrets,
    store,
)
from backend.core.clock import iso, now_utc
from backend.core.models import (
    Channel,
    Direction,
    EventKind,
    MessageKind,
    Player,
    PlayerStatus,
)
from backend.core.safety import Command

SECRET_HEADER = "x-telegram-bot-api-secret-token"

_OK = {"statusCode": 200, "body": "ok"}
_UNAUTHORISED = {"statusCode": 401, "body": "unauthorized"}


def handler(event: dict, _context: Any = None) -> dict:
    if not _authentic(event):
        logs.warn("telegram.webhook.unauthorised", source_ip=_source_ip(event))
        return _UNAUTHORISED

    try:
        update = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        logs.error("telegram.webhook.bad_json")
        return _OK

    try:
        _handle_update(update)
    except Exception as exc:
        logs.error(
            "telegram.webhook.failed",
            error=f"{type(exc).__name__}: {exc}",
            update_id=update.get("update_id"),
        )
    return _OK


# ----------------------------------------------------------------- security


def _headers(event: dict) -> dict[str, str]:
    return {k.lower(): v for k, v in (event.get("headers") or {}).items()}


def _source_ip(event: dict) -> str | None:
    return ((event.get("requestContext") or {}).get("http") or {}).get("sourceIp")


def _authentic(event: dict) -> bool:
    """Constant-time comparison of Telegram's secret-token header."""
    presented = _headers(event).get(SECRET_HEADER, "")
    expected = secrets.telegram_webhook_secret()
    return bool(presented) and hmac.compare_digest(presented, expected)


# ------------------------------------------------------------------ routing


def _handle_update(update: dict) -> None:
    message = tg.extract_message(update)
    if not message:
        return

    described = tg.describe_update(update)
    chat_id = described["chat_id"]
    text = described["text"]
    if chat_id is None:
        return

    # Claim the update before doing any work. Generation turned this handler
    # from one that answered in milliseconds into one that can take several
    # seconds, and Telegram retries anything it does not get a 2xx for -- a
    # retry arriving mid-generation would answer the same message twice and
    # spend two of the player's six daily messages on it.
    #
    # The trade is deliberate: an update that is claimed and then fails is not
    # retried. A dropped reply is recoverable and visible in the log; a
    # duplicate one is neither.
    update_id = described.get("update_id")
    if update_id is not None:
        try:
            store.claim_update(Channel.TELEGRAM, update_id)
        except store.AlreadyHandled:
            logs.info("telegram.duplicate_update", update_id=update_id)
            return

    command, argument = safety.parse_command(text)
    player_id = store.player_id_for_chat(Channel.TELEGRAM, chat_id)

    if player_id is None:
        _handle_unenrolled(chat_id, command, argument)
        return

    # A bound chat can still be moved to a different player, but only with a
    # code the operator minted -- which is what makes this an operator action
    # and not self-signup. Without this a chat is stuck on its first player
    # forever, so a finished player could never start a second arc.
    if command is Command.START and argument:
        target = store.player_id_for_code(argument)
        if target and target != player_id:
            _rebind(chat_id, previous=player_id, target=target, code=argument)
            return

    player = store.get_player(player_id)
    if player is None:
        logs.error("player.missing_for_chat", chat_id=chat_id, player_id=player_id)
        dispatch.send_system_to_chat(chat_id, safety.UNKNOWN_CHAT)
        return

    _record_inbound(player, described, command)
    _dispatch_command(player, command, argument, described)


def _handle_unenrolled(chat_id: int, command: Command, argument: str | None) -> None:
    """A chat with no player behind it.

    Enrolment is operator-driven, not self-signup: a player can only bind their
    chat with a code the operator minted for them out of band.
    """
    if command is Command.START and argument:
        player_id = store.player_id_for_code(argument)
        if player_id:
            _enrol(chat_id, player_id, argument)
            return
        logs.warn("enrolment.bad_code", chat_id=chat_id)
        dispatch.send_system_to_chat(chat_id, safety.BAD_CODE)
        return

    logs.info("telegram.unenrolled_chat", chat_id=chat_id, command=str(command))
    dispatch.send_system_to_chat(chat_id, safety.UNKNOWN_CHAT)


def _rebind(chat_id: int, *, previous: str, target: str, code: str) -> None:
    """Move a chat from one player to another.

    Logged loudly: if the previous player was still running, this has just cut
    off their only channel, and the operator should see that in the timeline
    rather than discover it when a beat fails to arrive.
    """
    old_player = store.get_player(previous)
    still_running = old_player is not None and not old_player.is_stopped
    logs.warn(
        "enrolment.rebind",
        chat_id=chat_id,
        previous_player_id=previous,
        target_player_id=target,
        previous_was_running=still_running,
    )

    store.unbind_chat(previous)
    store.record_event(
        previous,
        direction=Direction.OUT,
        channel=Channel.ADMIN,
        kind=EventKind.SYSTEM,
        text=f"Telegram chat moved to {target}. This player is no longer reachable.",
        source="enrolment",
    )
    _enrol(chat_id, target, code)


def _enrol(chat_id: int, player_id: str, code: str) -> None:
    player = store.get_player(player_id)
    if player is None:
        dispatch.send_system_to_chat(chat_id, safety.BAD_CODE)
        return

    player.telegram_chat_id = chat_id
    player.status = PlayerStatus.ACTIVE
    player.last_contact_at = iso(now_utc())
    # The arc starts when the player actually arrives, not when the operator
    # minted their code -- a code sent on Monday and redeemed on Thursday would
    # otherwise drop them into day four of a story they have not read.
    player.arc_id = player.arc_id or config.DEFAULT_ARC_ID
    player.arc_started_at = player.arc_started_at or iso(now_utc())
    store.put_player(player)
    store.bind_chat(Channel.TELEGRAM, chat_id, player_id)
    store.consume_enrolment_code(code)

    logs.info("enrolment.complete", player_id=player_id, chat_id=chat_id)
    store.record_event(
        player_id,
        direction=Direction.IN,
        channel=Channel.TELEGRAM,
        kind=EventKind.COMMAND,
        text="/start",
        source="player",
    )
    dispatch.send_to_player(
        player, safety.ENROLLED, kind=MessageKind.SYSTEM_REPLY, source="enrolment"
    )


def _record_inbound(player: Player, described: dict, command: Command) -> None:
    kind = EventKind.MESSAGE
    if described["has_photo"]:
        kind = EventKind.PHOTO
    elif described["has_location"]:
        kind = EventKind.LOCATION
    elif command is not Command.NONE:
        kind = EventKind.COMMAND

    # A message that reads as distress or a sincere "is this real?" does not
    # act on its own -- it raises a flag for a human to read.
    needs_review = (
        safety.mentions_stopping(described["text"]) and command is Command.NONE
    )

    store.record_event(
        player.player_id,
        direction=Direction.IN,
        channel=Channel.TELEGRAM,
        kind=kind,
        text=described["text"],
        telegram_message_id=described["message_id"],
        photo_file_id=described.get("photo_file_id"),
        needs_review=needs_review or None,
        source="player",
    )
    store.touch_last_contact(player.player_id)

    if needs_review:
        logs.warn("player.needs_review", player_id=player.player_id)


def _dispatch_command(
    player: Player, command: Command, argument: str | None, described: dict
) -> None:
    match command:
        case Command.STOP:
            control.stop(player)
        case Command.PAUSE:
            control.pause(player)
        case Command.RESUME:
            control.resume(player)
        case Command.REAL:
            dispatch.send_to_player(
                player,
                safety.REAL_TEXT,
                kind=MessageKind.SYSTEM_REPLY,
                source="command",
            )
        case Command.START:
            # Already enrolled; re-send the plain statement rather than
            # restarting anything.
            dispatch.send_to_player(
                player,
                safety.REAL_TEXT,
                kind=MessageKind.SYSTEM_REPLY,
                source="command",
            )
        case _:
            engine.respond(player, described)
