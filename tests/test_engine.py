"""The story engine.

Two things are being pinned down here. The first is that generated content is
*less* privileged than anything else in the system, not more: it passes the
same four gates an operator's hand-typed message does, and it carries an extra
review pass on top. The second is that every way generation can fail ends with
the player getting something sane and the operator getting a flag -- a broken
model must never look, from the player's side, like a story that stopped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.core import config, dispatch, engine, model, safety, store
from backend.core.clock import iso
from backend.core.models import BlockReason, Channel, Direction, EventKind, PlayerStatus

DAYTIME = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)  # 15:00 London
NIGHT = datetime(2026, 3, 10, 23, 30, tzinfo=UTC)  # 23:30 London

TEXT = "found the bench"


@pytest.fixture
def enrolled(player, table):
    """A player whose arc started today."""
    player.arc_id = config.DEFAULT_ARC_ID
    player.arc_started_at = iso(DAYTIME)
    store.put_player(player)
    return player


def inbound(player, text: str = TEXT) -> None:
    store.record_event(
        player.player_id,
        direction=Direction.IN,
        channel=Channel.TELEGRAM,
        kind=EventKind.MESSAGE,
        text=text,
        source="player",
    )


# ------------------------------------------------------------- happy path


def test_the_generated_reply_reaches_the_player(
    enrolled, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    story.text = "Wren here. Which bench?"
    inbound(enrolled)

    result = engine.respond(enrolled, {})

    assert result.sent
    assert story.text in telegram.last


def test_the_reply_carries_the_out_of_character_footer(
    enrolled, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    inbound(enrolled)
    engine.respond(enrolled, {})

    assert safety.FOOTER in telegram.last


def test_the_send_is_attributed_to_the_engine_and_the_beat(
    enrolled, telegram, frozen
) -> None:
    frozen(DAYTIME)
    inbound(enrolled)
    engine.respond(enrolled, {})

    sent = [e for e in store.timeline(enrolled.player_id) if e["direction"] == "out"]
    assert sent[-1]["source"] == "engine"
    assert sent[-1]["beat_id"] == "d1-wrong-number"


# ------------------------------------------ the engine has no special powers
#
# These four mirror the Phase 2 tests that pinned the same properties for an
# operator typing by hand. If generated content could bypass a gate, the
# safety layer would only be protecting the story from the operator.


def test_quiet_hours_hold_a_generated_message(enrolled, telegram, frozen) -> None:
    frozen(NIGHT)
    inbound(enrolled)

    result = engine.respond(enrolled, {})

    assert result.blocked
    assert result.reason is BlockReason.QUIET_HOURS
    assert telegram.sent == []


def test_the_daily_limit_applies_to_generated_messages(
    enrolled, telegram, frozen
) -> None:
    frozen(DAYTIME)
    for _ in range(config.MAX_STORY_MESSAGES_PER_DAY):
        dispatch.send_to_player(enrolled, "filler")
    telegram.sent.clear()

    inbound(enrolled)
    result = engine.respond(enrolled, {})

    assert result.blocked
    assert result.reason is BlockReason.RATE_LIMITED
    assert telegram.sent == []


def test_a_stopped_player_gets_nothing_from_the_engine(
    enrolled, telegram, frozen
) -> None:
    frozen(DAYTIME)
    store.update_player_status(enrolled.player_id, PlayerStatus.STOPPED)
    enrolled.status = PlayerStatus.STOPPED

    result = engine.respond(enrolled, {})

    assert result.blocked
    assert telegram.sent == []


def test_the_deterministic_rules_still_apply_to_generated_text(
    enrolled, telegram, frozen, story
) -> None:
    """The regex layer runs on generated content too, and it cannot be argued with.

    The reviewer here is deliberately made to approve the message, so the only
    thing standing between it and the player is the layer that does not think.
    """
    frozen(DAYTIME)
    story.text = "Call 999 right now, he's bleeding."
    story.verdict = {"safe": True, "rules": [], "reason": "ok"}
    inbound(enrolled)

    engine.respond(enrolled, {})

    assert "999" not in telegram.last
    assert safety.SAFE_FALLBACK in telegram.last


# ------------------------------------------------------------- the review pass


def test_a_refused_message_never_reaches_the_player(
    enrolled, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    story.text = "I am a real person, none of this is made up."
    story.refuse("denies_fiction", reason="claims to be human")
    inbound(enrolled)

    engine.respond(enrolled, {})

    assert story.text not in (telegram.last or "")
    assert safety.SAFE_FALLBACK in telegram.last


def test_a_refused_message_is_kept_on_the_timeline_for_the_operator(
    enrolled, telegram, frozen, story
) -> None:
    """The operator has to be able to read what was refused, not just its rule id."""
    frozen(DAYTIME)
    story.text = "I am a real person."
    story.refuse("denies_fiction", reason="claims to be human")
    inbound(enrolled)

    engine.respond(enrolled, {})

    blocked = [
        e for e in store.timeline(enrolled.player_id) if e["kind"] == EventKind.BLOCKED
    ]
    assert blocked[-1]["text"] == story.text
    assert blocked[-1]["reason"] == "review_refused"
    assert blocked[-1]["needs_review"] is True
    assert blocked[-1]["review_reason"] == "claims to be human"


def test_a_verdict_that_says_safe_but_names_rules_is_treated_as_a_refusal(
    enrolled, telegram, frozen, story
) -> None:
    """Trusting the boolean over the rules is the dangerous way to resolve this."""
    frozen(DAYTIME)
    story.text = "Some prose."
    story.verdict = {
        "safe": True,
        "rules": ["impersonation"],
        "reason": "claims police",
    }
    inbound(enrolled)

    engine.respond(enrolled, {})

    assert story.text not in (telegram.last or "")


def test_an_unreachable_reviewer_does_not_stop_the_story(
    enrolled, telegram, frozen, story
) -> None:
    """A throttled reviewer must not silently replace the week with a fallback."""
    frozen(DAYTIME)
    story.text = "Wren here."
    story.review_error = model.ModelUnavailable("429")
    inbound(enrolled)

    engine.respond(enrolled, {})

    assert story.text in telegram.last


def test_an_unreachable_reviewer_is_flagged_for_the_operator(
    enrolled, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    story.review_error = model.ModelUnavailable("429")
    inbound(enrolled)

    engine.respond(enrolled, {})

    flagged = [
        e for e in store.timeline(enrolled.player_id) if e.get("needs_review") is True
    ]
    assert flagged, "an unreviewed send must be visible in the admin panel"


# ------------------------------------------------------------ generation fails


def test_a_model_failure_falls_back_instead_of_going_silent(
    enrolled, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    story.error = model.ModelUnavailable("timeout")
    inbound(enrolled)

    result = engine.respond(enrolled, {})

    assert result.sent
    assert safety.SAFE_FALLBACK in telegram.last


def test_a_model_failure_is_recorded_as_an_error_for_the_operator(
    enrolled, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    story.error = model.ModelUnavailable("timeout")
    inbound(enrolled)

    engine.respond(enrolled, {})

    errors = [
        e for e in store.timeline(enrolled.player_id) if e["kind"] == EventKind.ERROR
    ]
    assert errors and errors[-1]["needs_review"] is True


def test_the_fallback_is_held_by_quiet_hours_like_any_story_message(
    enrolled, telegram, frozen, story
) -> None:
    """Otherwise a failing model becomes a way to message someone at 23:30."""
    frozen(NIGHT)
    story.error = model.ModelUnavailable("timeout")
    inbound(enrolled)

    result = engine.respond(enrolled, {})

    assert result.blocked
    assert telegram.sent == []


def test_a_broken_arc_file_does_not_reach_the_player(
    enrolled, telegram, frozen, monkeypatch
) -> None:
    from backend.story.arc import ArcError

    frozen(DAYTIME)

    def broken(_player):
        raise ArcError("nightjar.yaml: two beats share a day")

    monkeypatch.setattr(engine, "arc_for", broken)
    inbound(enrolled)

    result = engine.respond(enrolled, {})

    assert result.sent
    assert safety.SAFE_FALLBACK in telegram.last


# ------------------------------------------------------------------- context


def test_the_current_beat_follows_the_calendar(enrolled, table, frozen) -> None:
    frozen(DAYTIME)
    ctx = engine.build_context(enrolled, now=DAYTIME + timedelta(days=3))

    assert ctx.day == 4
    assert ctx.beat.day == 4
    assert ctx.beat.beat_id == "d4-m-hallam"


def test_a_player_past_the_end_of_the_arc_stays_on_the_last_beat(
    enrolled, table
) -> None:
    ctx = engine.build_context(enrolled, now=DAYTIME + timedelta(days=40))

    assert ctx.beat.beat_id == "d7-nightjar"


def test_the_system_prompt_carries_todays_beat_and_not_tomorrows(
    enrolled, table
) -> None:
    ctx = engine.build_context(enrolled, now=DAYTIME)
    prompt = engine._system_prompt(enrolled, ctx, now=DAYTIME)

    assert "A number in a logbook" in prompt
    assert "Thursdays, always" not in prompt


def test_the_system_prompt_tells_the_writer_how_many_messages_are_left(
    enrolled, telegram, frozen
) -> None:
    frozen(DAYTIME)
    dispatch.send_to_player(enrolled, "one")
    ctx = engine.build_context(enrolled, now=DAYTIME)

    prompt = engine._system_prompt(enrolled, ctx, now=DAYTIME)
    assert f"You have {config.MAX_STORY_MESSAGES_PER_DAY - 1} of today's" in prompt


def test_the_conversation_is_replayed_in_order_with_roles(
    enrolled, telegram, frozen
) -> None:
    frozen(DAYTIME)
    inbound(enrolled, "hello")
    dispatch.send_to_player(enrolled, "Wren here.")
    inbound(enrolled, "who is this")

    ctx = engine.build_context(enrolled, now=DAYTIME)

    assert [m["role"] for m in ctx.history] == ["user", "assistant", "user"]
    assert ctx.history[0]["content"] == "hello"
    assert ctx.history[2]["content"] == "who is this"


def test_the_footer_is_stripped_before_the_model_sees_its_own_words(
    enrolled, telegram, frozen
) -> None:
    """Left in, the model learns to write a footer, and dispatch then adds a second."""
    frozen(DAYTIME)
    dispatch.send_to_player(enrolled, "Wren here.")
    inbound(enrolled, "hi")

    ctx = engine.build_context(enrolled, now=DAYTIME)

    assert safety.FOOTER in telegram.last
    assert all(safety.FOOTER not in m["content"] for m in ctx.history)


def test_system_replies_and_commands_are_not_replayed_as_story(
    enrolled, telegram, frozen
) -> None:
    """Replaying them teaches the model to write STOP confirmations."""
    frozen(DAYTIME)
    inbound(enrolled, "hi")
    dispatch.send_to_player(
        enrolled, safety.REAL_TEXT, kind=engine.MessageKind.SYSTEM_REPLY
    )

    ctx = engine.build_context(enrolled, now=DAYTIME)

    assert [m["role"] for m in ctx.history] == ["user"]


def test_only_the_most_recent_events_are_replayed(enrolled, telegram, frozen) -> None:
    """Ascending with a limit would have replayed the oldest forty instead."""
    frozen(DAYTIME)
    for i in range(config.CONTEXT_EVENT_LIMIT + 10):
        inbound(enrolled, f"message {i}")

    ctx = engine.build_context(enrolled, now=DAYTIME)

    assert len(ctx.history) == config.CONTEXT_EVENT_LIMIT
    assert ctx.history[-1]["content"] == f"message {config.CONTEXT_EVENT_LIMIT + 9}"


def test_the_replay_never_opens_on_the_story_speaking(
    enrolled, telegram, frozen
) -> None:
    """The API requires the first turn to be the player's."""
    frozen(DAYTIME)
    dispatch.send_to_player(enrolled, "Wren here, sorry to bother you.")
    inbound(enrolled, "who is this")

    ctx = engine.build_context(enrolled, now=DAYTIME)

    assert ctx.history[0]["role"] == "user"


# ------------------------------------------------------------- attachments


def test_a_photo_is_acknowledged_without_pretending_to_have_seen_it(
    enrolled, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    store.record_event(
        enrolled.player_id,
        direction=Direction.IN,
        channel=Channel.TELEGRAM,
        kind=EventKind.PHOTO,
        text=None,
        source="player",
    )

    engine.respond(enrolled, {"has_photo": True})

    direction = story.last_messages[-1]["content"]
    assert "cannot see it" in direction


def test_a_shared_location_is_never_turned_into_a_place(
    enrolled, telegram, frozen, story
) -> None:
    """Coordinates are dropped at the edge, so the model must not guess at them."""
    frozen(DAYTIME)
    store.record_event(
        enrolled.player_id,
        direction=Direction.IN,
        channel=Channel.TELEGRAM,
        kind=EventKind.LOCATION,
        source="player",
    )

    engine.respond(enrolled, {"has_location": True})

    direction = story.last_messages[-1]["content"]
    assert "must not guess" in direction


# --------------------------------------------------------------- opening a beat


def test_opening_a_beat_works_with_no_conversation_yet(
    enrolled, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    story.text = "Sorry -- wrong number, probably."

    result = engine.open_beat(enrolled, now=DAYTIME)

    assert result.sent
    assert story.text in telegram.last


def test_opening_a_beat_records_it(enrolled, telegram, frozen) -> None:
    frozen(DAYTIME)
    engine.open_beat(enrolled, now=DAYTIME)

    beat = store.get_beat(enrolled.player_id, "d1-wrong-number")
    assert beat and beat["opened_at"]
