"""STOP / PAUSE / RESUME state transitions.

The invariant under test throughout: the state change is durable before the
acknowledgement is attempted, so a failure to reach the player can never leave
a story running that the player asked to end.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.core import control, dispatch, safety, store
from backend.core.models import PlayerStatus

DAYTIME = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
NIGHT = datetime(2026, 3, 10, 23, 30, tzinfo=UTC)


# ---------------------------------------------------------------- stop


def test_stop_halts_the_story_and_confirms_once(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    control.stop(player)

    assert store.get_player(player.player_id).status is PlayerStatus.STOPPED
    assert len(telegram.sent) == 1
    assert telegram.last == safety.STOP_CONFIRMATION


def test_stop_works_during_quiet_hours(player, telegram, frozen) -> None:
    """ "Must work at any point" outranks the 22:00-08:00 window."""
    frozen(NIGHT)
    result = control.stop(player)

    assert result.sent
    assert store.get_player(player.player_id).status is PlayerStatus.STOPPED


def test_stop_works_when_the_daily_allowance_is_spent(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    for i in range(10):
        dispatch.send_to_player(player, f"beat {i}")

    result = control.stop(player)
    assert result.sent
    assert telegram.last == safety.STOP_CONFIRMATION


def test_stop_cancels_scheduled_work_before_changing_state(
    player, telegram, frozen, monkeypatch
) -> None:
    """Phase 3 fills this hook in; the call site is asserted on now."""
    calls: list[str] = []
    monkeypatch.setattr(
        control,
        "cancel_scheduled_work",
        lambda pid, reason: calls.append(pid) or {"executions": 0, "schedules": 0},
    )
    frozen(DAYTIME)
    control.stop(player)

    assert calls == [player.player_id]


def test_the_terminal_state_is_persisted_even_if_the_reply_fails(
    player, telegram, frozen, monkeypatch
) -> None:
    frozen(DAYTIME)

    def explode(*_args, **_kwargs):
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(telegram, "send_message", explode)

    with pytest.raises(RuntimeError):
        control.stop(player)

    assert store.get_player(player.player_id).status is PlayerStatus.STOPPED


def test_stopping_twice_says_so_rather_than_re_stopping(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    control.stop(player)
    telegram.sent.clear()

    control.stop(player)
    assert telegram.last == safety.ALREADY_STOPPED


def test_a_stopped_player_receives_no_further_story_content(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    control.stop(player)
    telegram.sent.clear()

    assert dispatch.send_to_player(player, "day 4 begins").blocked
    assert telegram.sent == []


def test_the_stop_is_recorded_with_a_reason(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    control.stop(player, reason="operator intervened", source="admin")

    stored = store.get_player(player.player_id)
    assert stored.stop_reason == "operator intervened"
    assert stored.stopped_at is not None


# --------------------------------------------------------------- pause


def test_pause_suspends_and_resume_continues(player, telegram, frozen) -> None:
    frozen(DAYTIME)

    control.pause(player)
    assert store.get_player(player.player_id).status is PlayerStatus.PAUSED
    assert dispatch.send_to_player(player, "day 4 begins").blocked

    control.resume(player)
    assert store.get_player(player.player_id).status is PlayerStatus.ACTIVE
    assert dispatch.send_to_player(player, "day 4 begins").sent


def test_pause_works_during_quiet_hours(player, telegram, frozen) -> None:
    frozen(NIGHT)
    assert control.pause(player).sent


def test_resume_after_stop_is_refused(player, telegram, frozen) -> None:
    """STOP is terminal. Resuming would be the worst possible surprise."""
    frozen(DAYTIME)
    control.stop(player)
    telegram.sent.clear()

    control.resume(player)

    assert store.get_player(player.player_id).status is PlayerStatus.STOPPED
    assert telegram.last == safety.RESUME_AFTER_STOP


def test_resume_when_not_paused_is_a_no_op(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    control.resume(player)

    assert store.get_player(player.player_id).status is PlayerStatus.ACTIVE
    assert telegram.last == safety.RESUME_WHEN_NOT_PAUSED


def test_pause_after_stop_is_refused(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    control.stop(player)
    telegram.sent.clear()

    control.pause(player)
    assert store.get_player(player.player_id).status is PlayerStatus.STOPPED
    assert telegram.last == safety.ALREADY_STOPPED


# ------------------------------------------------------------ timeline hygiene


def test_a_player_stop_is_not_double_recorded(player, telegram, frozen) -> None:
    """The webhook already logged what they typed; a second row would misreport it."""
    from backend.core.models import Channel, Direction, EventKind

    frozen(DAYTIME)
    store.record_event(
        player.player_id,
        direction=Direction.IN,
        channel=Channel.TELEGRAM,
        kind=EventKind.COMMAND,
        text="please stop",
        source="player",
    )
    control.stop(player)

    commands = [e for e in store.timeline(player.player_id) if e["kind"] == "command"]
    assert [e["text"] for e in commands] == ["please stop"]


def test_an_operator_stop_is_recorded_as_an_admin_action(
    player, telegram, frozen
) -> None:
    """Nothing else logs it, so this path must."""
    frozen(DAYTIME)
    control.stop(player, reason="operator call", source="admin")

    commands = [e for e in store.timeline(player.player_id) if e["kind"] == "command"]
    assert len(commands) == 1
    assert commands[0]["text"] == "STOP"
    assert commands[0]["channel"] == "admin"
    assert commands[0]["direction"] == "out"
    assert commands[0]["reason"] == "operator call"
