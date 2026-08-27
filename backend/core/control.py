"""STOP / PAUSE / RESUME.

These are the player's controls over the experience and they are the one part
of the system that must never fail silently. Each function does the state
change first and the acknowledgement second: if the outbound message fails, the
story is still halted.
"""

from __future__ import annotations

from backend.core import dispatch, logs, safety, store
from backend.core.models import (
    Channel,
    Direction,
    EventKind,
    MessageKind,
    Player,
    PlayerStatus,
    SendResult,
)


def cancel_scheduled_work(player_id: str, *, reason: str) -> dict[str, int]:
    """Tear down everything that could still reach this player.

    Phase 1 has nothing to cancel yet -- there is no Step Functions execution
    and no EventBridge schedule until Phase 3. This function exists now, is
    called from the real stop path now, and is asserted on by the tests now,
    so that Phase 3 fills in a body rather than remembering to add a call.

    Returns a count per resource type, for the log line.
    """
    cancelled = {"executions": 0, "schedules": 0}
    # Phase 3: stepfunctions.stop_execution(...) for the player's arc
    # Phase 3: scheduler.delete_schedule(...) for every pending beat
    # Phase 6: stop any in-flight Fargate call task
    logs.info(
        "control.cancel_scheduled_work", player_id=player_id, reason=reason, **cancelled
    )
    return cancelled


def stop(
    player: Player, *, reason: str = "player requested", source: str = "player"
) -> SendResult:
    """Halt the story immediately and confirm once, out of character.

    Ordering matters: cancel first, persist the terminal status second,
    acknowledge last. A crash at any point leaves the player stopped, never
    half-stopped.
    """
    if player.is_stopped:
        return dispatch.send_to_player(
            player,
            safety.ALREADY_STOPPED,
            kind=MessageKind.SYSTEM_REPLY,
            source=source,
        )

    cancel_scheduled_work(player.player_id, reason=reason)
    store.update_player_status(player.player_id, PlayerStatus.STOPPED, reason=reason)
    player.status = PlayerStatus.STOPPED

    logs.warn(
        "control.stopped", player_id=player.player_id, reason=reason, source=source
    )
    # Only the operator path records the command. A player-initiated one is
    # already on the timeline as the inbound message they actually typed --
    # recording a second row saying "STOP" would misreport their words.
    if source != "player":
        store.record_event(
            player.player_id,
            direction=Direction.OUT,
            channel=Channel.ADMIN,
            kind=EventKind.COMMAND,
            text="STOP",
            reason=reason,
            source=source,
        )

    return dispatch.send_to_player(
        player,
        safety.STOP_CONFIRMATION,
        kind=MessageKind.SYSTEM_REPLY,
        source=source,
    )


def pause(player: Player, *, source: str = "player") -> SendResult:
    if player.is_stopped:
        return dispatch.send_to_player(
            player, safety.ALREADY_STOPPED, kind=MessageKind.SYSTEM_REPLY, source=source
        )

    store.update_player_status(player.player_id, PlayerStatus.PAUSED)
    player.status = PlayerStatus.PAUSED
    logs.info("control.paused", player_id=player.player_id, source=source)
    # Only the operator path records the command. A player-initiated one is
    # already on the timeline as the inbound message they actually typed --
    # recording a second row saying "STOP" would misreport their words.
    if source != "player":
        store.record_event(
            player.player_id,
            direction=Direction.OUT,
            channel=Channel.ADMIN,
            kind=EventKind.COMMAND,
            text="PAUSE",
            source=source,
        )
    return dispatch.send_to_player(
        player, safety.PAUSE_CONFIRMATION, kind=MessageKind.SYSTEM_REPLY, source=source
    )


def resume(player: Player, *, source: str = "player") -> SendResult:
    """Continue a paused story. A stopped story is terminal and does not resume."""
    if player.is_stopped:
        return dispatch.send_to_player(
            player,
            safety.RESUME_AFTER_STOP,
            kind=MessageKind.SYSTEM_REPLY,
            source=source,
        )
    if player.status is not PlayerStatus.PAUSED:
        return dispatch.send_to_player(
            player,
            safety.RESUME_WHEN_NOT_PAUSED,
            kind=MessageKind.SYSTEM_REPLY,
            source=source,
        )

    store.update_player_status(player.player_id, PlayerStatus.ACTIVE)
    player.status = PlayerStatus.ACTIVE
    logs.info("control.resumed", player_id=player.player_id, source=source)
    # Only the operator path records the command. A player-initiated one is
    # already on the timeline as the inbound message they actually typed --
    # recording a second row saying "STOP" would misreport their words.
    if source != "player":
        store.record_event(
            player.player_id,
            direction=Direction.OUT,
            channel=Channel.ADMIN,
            kind=EventKind.COMMAND,
            text="RESUME",
            source=source,
        )
    return dispatch.send_to_player(
        player, safety.RESUME_CONFIRMATION, kind=MessageKind.SYSTEM_REPLY, source=source
    )
