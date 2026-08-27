"""The outbound chokepoint.

Every gate is tested for both outcomes, and the interactions between them are
tested too -- particularly that a blocked message does not spend one of the
player's six, since that would silently shrink the story.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.core import config, dispatch, store
from backend.core.models import (
    BlockReason,
    MessageKind,
    PlayerStatus,
)

DAYTIME = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)  # 15:00 London
NIGHT = datetime(2026, 3, 10, 23, 30, tzinfo=UTC)  # 23:30 London


# ----------------------------------------------------------- happy path


def test_a_story_message_reaches_the_player(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    result = dispatch.send_to_player(player, "The envelope is under the bench.")

    assert result.sent
    assert len(telegram.sent) == 1
    chat_id, text = telegram.sent[0]
    assert chat_id == 42
    assert "The envelope is under the bench." in text


def test_the_send_is_recorded_on_the_timeline(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    dispatch.send_to_player(player, "Bring it to the fountain.")

    events = store.timeline(player.player_id)
    assert len(events) == 1
    assert events[0]["direction"] == "out"
    assert events[0]["kind"] == "message"


def test_typing_indicator_precedes_an_in_character_reply(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    dispatch.send_to_player(player, "Wait for the bell.")
    assert telegram.actions == [(42, "typing")]


def test_system_replies_are_instant_with_no_typing_theatre(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    dispatch.send_to_player(player, "Stopped.", kind=MessageKind.SYSTEM_REPLY)
    assert telegram.actions == []


# ------------------------------------------------------- gate 1: status


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (PlayerStatus.STOPPED, BlockReason.PLAYER_STOPPED),
        (PlayerStatus.PAUSED, BlockReason.PLAYER_PAUSED),
        (PlayerStatus.PENDING, BlockReason.PLAYER_PAUSED),
    ],
)
def test_story_content_never_reaches_a_non_active_player(
    player, telegram, frozen, status, reason
) -> None:
    frozen(DAYTIME)
    player.status = status

    result = dispatch.send_to_player(player, "Are you there?")

    assert result.blocked
    assert result.reason is reason
    assert telegram.sent == []


@pytest.mark.parametrize(
    "status", [PlayerStatus.STOPPED, PlayerStatus.PAUSED, PlayerStatus.PENDING]
)
def test_system_replies_reach_a_player_in_any_state(
    player, telegram, frozen, status
) -> None:
    """A STOP confirmation must arrive even though the player is stopped."""
    frozen(DAYTIME)
    player.status = status

    result = dispatch.send_to_player(player, "Stopped.", kind=MessageKind.SYSTEM_REPLY)

    assert result.sent
    assert telegram.texts == ["Stopped."]


def test_a_player_with_no_chat_bound_is_not_reachable(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    player.telegram_chat_id = None

    result = dispatch.send_to_player(player, "hello?")

    assert result.reason is BlockReason.NO_CHANNEL
    assert telegram.sent == []


# -------------------------------------------------- gate 2: quiet hours


def test_story_content_is_held_during_quiet_hours(player, telegram, frozen) -> None:
    frozen(NIGHT)
    result = dispatch.send_to_player(player, "Are you awake?")

    assert result.blocked
    assert result.reason is BlockReason.QUIET_HOURS
    assert telegram.sent == []


def test_a_held_message_says_when_it_could_be_sent(player, telegram, frozen) -> None:
    """Phase 3 reschedules on this rather than dropping the beat."""
    frozen(NIGHT)
    result = dispatch.send_to_player(player, "Are you awake?")

    assert result.retry_at is not None
    assert result.retry_at.startswith("2026-03-11T08:00")


def test_quiet_hours_do_not_silence_a_stop_confirmation(
    player, telegram, frozen
) -> None:
    """A player who texts at 23:30 is demonstrably awake and is owed an answer."""
    frozen(NIGHT)
    result = dispatch.send_to_player(player, "Stopped.", kind=MessageKind.SYSTEM_REPLY)

    assert result.sent
    assert telegram.texts == ["Stopped."]


def test_a_message_held_for_quiet_hours_costs_nothing(player, telegram, frozen) -> None:
    frozen(NIGHT)
    dispatch.send_to_player(player, "Are you awake?")
    assert store.quota_used(player.player_id, player.timezone) == 0


# --------------------------------------------------- gate 3: validation


def test_a_message_that_breaks_a_content_rule_is_replaced_not_sent(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    result = dispatch.send_to_player(player, "This is the police. Send your password.")

    assert result.sent  # the story still moves
    assert result.reason is BlockReason.VALIDATION_FAILED
    assert "police" not in telegram.last
    assert "password" not in telegram.last


def test_a_validation_failure_is_flagged_for_the_operator(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    dispatch.send_to_player(player, "Transfer $500 to the account below.")

    flagged = [e for e in store.timeline(player.player_id) if e.get("needs_review")]
    assert len(flagged) == 1
    assert flagged[0]["reason"] == "validation_failed"
    assert "credential_or_money" in flagged[0]["rules"]
    # The original text is retained so a human can see what was nearly sent.
    assert "$500" in flagged[0]["text"]


# --------------------------------------------------- gate 4: rate limit


def test_a_player_gets_six_story_messages_a_day(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    for i in range(config.MAX_STORY_MESSAGES_PER_DAY):
        assert dispatch.send_to_player(player, f"beat {i}").sent

    seventh = dispatch.send_to_player(player, "one too many")
    assert seventh.blocked
    assert seventh.reason is BlockReason.RATE_LIMITED
    assert len(telegram.sent) == config.MAX_STORY_MESSAGES_PER_DAY


def test_the_rate_limit_does_not_apply_to_system_replies(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    for i in range(config.MAX_STORY_MESSAGES_PER_DAY):
        dispatch.send_to_player(player, f"beat {i}")

    result = dispatch.send_to_player(player, "Stopped.", kind=MessageKind.SYSTEM_REPLY)
    assert result.sent


def test_the_allowance_is_counted_in_the_players_own_day(
    player, telegram, frozen
) -> None:
    """01:00 UTC is still yesterday in Denver, so the counter must not roll over."""
    player.timezone = "America/Denver"
    frozen(datetime(2026, 3, 10, 20, 0, tzinfo=UTC))  # 13:00 Denver
    dispatch.send_to_player(player, "afternoon")

    frozen(datetime(2026, 3, 11, 1, 0, tzinfo=UTC))  # 18:00 Denver, same local day
    dispatch.send_to_player(player, "evening")

    assert store.quota_used(player.player_id, player.timezone) == 2


# ------------------------------------------------------------- footer


def test_the_first_story_message_of_the_day_carries_the_footer(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    dispatch.send_to_player(player, "It starts now.")
    assert "/real" in telegram.last
    assert "STOP" in telegram.last


def test_later_messages_that_day_do_not_repeat_it(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    dispatch.send_to_player(player, "first")
    dispatch.send_to_player(player, "second")
    assert telegram.texts[1] == "second"
