"""The story engine.

Replaces the Phase 1 echo. Given a player and what they just sent, this builds
the model's view of the story so far, generates one in-character reply, has it
reviewed, and hands it to :func:`dispatch.send_to_player` like any other
outbound message.

It has no special privileges. The engine's output goes through exactly the
same four gates an operator's hand-typed message does -- status, quiet hours,
content validation, rate limit -- and the review pass in :mod:`review` is an
*additional* burden it carries because its text is generated, not a
replacement for anything.

Which beat is current is arithmetic, not a decision: see
:func:`backend.core.clock.arc_day`. Nothing here asks the model how the story
is going, because a model that can talk itself into "the arc is finished on
day two" is a model that can end someone's week early.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.core import config, dispatch, logs, model, review, safety, store
from backend.core.clock import arc_day, iso, now_utc, parse_iso, to_local
from backend.core.models import (
    Channel,
    Direction,
    EventKind,
    MessageKind,
    Player,
    SendResult,
)
from backend.story.arc import Arc, ArcError, Beat, load_arc

#: Timeline event kinds that represent something the player actually saw or
#: said. Blocked sends, system replies and commands are excluded: replaying
#: them would teach the model to write STOP confirmations.
_PLAYER_VISIBLE = {EventKind.MESSAGE, EventKind.PHOTO, EventKind.LOCATION}


@dataclass(frozen=True)
class Context:
    arc: Arc
    beat: Beat
    day: int
    history: list[dict]

    @property
    def transcript(self) -> list[str]:
        """Recent turns as plain labelled lines, for the reviewer."""
        return [
            f"{'Player' if m['role'] == 'user' else 'Story'}: {m['content']}"
            for m in self.history
        ]


def arc_for(player: Player) -> Arc:
    return load_arc(player.arc_id or config.DEFAULT_ARC_ID)


def build_context(player: Player, *, now=None) -> Context:
    """Everything the model is allowed to know about this player's story."""
    now = now or now_utc()
    arc = arc_for(player)
    started = parse_iso(player.arc_started_at) if player.arc_started_at else now
    day = arc_day(started, now, player.timezone)
    return Context(
        arc=arc,
        beat=arc.beat_for_day(day),
        day=day,
        history=_history(player),
    )


def _history(player: Player) -> list[dict]:
    """Replay the timeline as an alternating conversation.

    The out-of-character footer is stripped on the way in. It is added by
    dispatch on the way out, and leaving it in the replay would teach the model
    to write its own -- which would then be appended to, and doubled.
    """
    # Newest-first with a limit, then reversed: querying ascending with a
    # limit would return the *oldest* forty events, so by day four the model
    # would be reading the start of the week and nothing since.
    events = store.timeline(
        player.player_id, limit=config.CONTEXT_EVENT_LIMIT, ascending=False
    )
    events.reverse()
    messages: list[dict] = []

    for event in events:
        kind = event.get("kind")
        if kind not in _PLAYER_VISIBLE:
            continue

        inbound = event.get("direction") == Direction.IN
        if not inbound and event.get("message_kind") != MessageKind.STORY:
            continue

        text = (event.get("text") or "").replace(safety.FOOTER, "").strip()
        if kind == EventKind.PHOTO:
            text = f"[sent a photo] {text}".strip()
        elif kind == EventKind.LOCATION:
            text = "[shared their location]"
        if not text:
            continue

        messages.append({"role": "user" if inbound else "assistant", "content": text})

    # The API requires the first turn to be the user's. A story that opened
    # proactively has an assistant message first, so drop the lead-in rather
    # than invent a player turn that never happened.
    while messages and messages[0]["role"] == "assistant":
        messages.pop(0)
    return messages


def _system_prompt(player: Player, ctx: Context, *, now=None) -> str:
    now = now or now_utc()
    local = to_local(now, player.timezone)
    used = store.quota_used(player.player_id, player.timezone)
    remaining = max(0, config.MAX_STORY_MESSAGES_PER_DAY - used)

    return f"""You are writing an interactive fiction story delivered over Telegram.

# The story

{ctx.arc.premise}

# Tone

{ctx.arc.tone}

# Cast

{ctx.arc.cast}

# Today

{ctx.beat.render()}

# Who you are writing to

{player.display_name}. Their local time is {local:%A %H:%M}. This is day
{ctx.day} of {ctx.arc.duration_days}. You have {remaining} of today's
{config.MAX_STORY_MESSAGES_PER_DAY} messages left, so pace the day
accordingly; if this is the last one, do not leave a question hanging on an
answer that cannot arrive until tomorrow.

# How to write

- Write one message, as the character, in plain text.
- Under 600 characters. Never over 1000.
- No markdown, no asterisks, no underscores -- they are shown literally.
- No greeting formula, no signature, no subject line.
- Do not add any disclaimer or footer. That is appended for you, and writing
  your own would double it.
- Do not narrate stage directions or describe yourself in the third person.
- Reply to what the player actually said. If they asked something, answer it.

# Rules you cannot write your way around

The player knows this is fiction and was told so at enrolment. Keep it that
way.

- Never claim to be a real person, deny being AI, or say the story is real.
- Never present anything as a real emergency in the player's own life, and
  never tell them to call anyone or seek help for a real situation.
- Never claim to be police, a bank, a hospital, a government body, an
  employer, or any other real institution.
- Never ask for money, payment, passwords, codes, documents, or account
  details.
- Never ask the player to go anywhere, enter anywhere, travel, drive, or
  approach, follow or photograph a person. Asking them to describe or
  photograph an ordinary object where they already are is fine.
- If the player sounds frightened, distressed, or sincerely asks whether this
  is real, stop the story immediately. Drop out of character, say plainly that
  it is a written story and that they can send STOP to end it, and do not
  return to the fiction in that message.

A message that breaks any of these is discarded before it reaches the player,
and the operator is alerted."""


def _generate(player: Player, ctx: Context, direction: str | None) -> str | None:
    """One model call plus its review. Returns None when nothing may be sent."""
    messages = list(ctx.history)
    if direction:
        messages.append({"role": "user", "content": f"[Direction: {direction}]"})
    if not messages:
        return None

    dispatch.show_typing(player)
    try:
        candidate = model.write(
            system=_system_prompt(player, ctx),
            messages=messages,
            purpose="story",
        )
    except model.ModelUnavailable as exc:
        logs.error(
            "engine.generation_failed",
            player_id=player.player_id,
            beat_id=ctx.beat.beat_id,
            error=str(exc),
        )
        store.record_event(
            player.player_id,
            direction=Direction.OUT,
            channel=Channel.TELEGRAM,
            kind=EventKind.ERROR,
            text=str(exc),
            beat_id=ctx.beat.beat_id,
            needs_review=True,
            source="engine",
        )
        return None

    verdict = review.review_outbound(candidate, recent=ctx.transcript)
    if not verdict.ok:
        # The message is never sent. It is kept on the timeline so the operator
        # can read exactly what was refused rather than guess from a rule id.
        store.record_event(
            player.player_id,
            direction=Direction.OUT,
            channel=Channel.TELEGRAM,
            kind=EventKind.BLOCKED,
            text=candidate,
            reason="review_refused",
            rules=list(verdict.rules),
            review_reason=verdict.reason,
            needs_review=True,
            beat_id=ctx.beat.beat_id,
            source="engine",
        )
        logs.warn(
            "engine.review_refused",
            player_id=player.player_id,
            beat_id=ctx.beat.beat_id,
            rules=list(verdict.rules),
        )
        return None

    if verdict.unreviewed:
        store.record_event(
            player.player_id,
            direction=Direction.OUT,
            channel=Channel.TELEGRAM,
            kind=EventKind.SYSTEM,
            text="Sent without a second-pass review: the reviewer was unreachable.",
            reason=verdict.reason,
            needs_review=True,
            beat_id=ctx.beat.beat_id,
            source="engine",
        )
    return candidate


def _send(player: Player, ctx: Context, text: str | None, *, source: str) -> SendResult:
    if text is None:
        # Sent as ordinary story content, not as a system reply. The fallback
        # stands in for the message that failed, so it inherits that message's
        # gates: it is held during quiet hours and it spends one of the six,
        # exactly as dispatch's own validation fallback does. Exempting it
        # would let a failing model talk past the quiet-hours rule.
        return dispatch.send_to_player(
            player,
            safety.SAFE_FALLBACK,
            beat_id=ctx.beat.beat_id,
            source=f"{source}:fallback",
        )

    result = dispatch.send_to_player(
        player,
        text,
        beat_id=ctx.beat.beat_id,
        source=source,
        # The wait already happened, in front of a live "typing…" indicator,
        # while the model was writing. Pausing again here would only make the
        # reply late.
        simulate_typing=False,
    )
    if result.sent:
        store.record_beat(
            player.player_id,
            ctx.beat.beat_id,
            day=ctx.beat.day,
            title=ctx.beat.title,
            last_sent_at=iso(now_utc()),
        )
    return result


def respond(player: Player, described: dict, *, now=None) -> SendResult:
    """Answer something the player just sent. The engine's main entry point."""
    try:
        ctx = build_context(player, now=now)
    except ArcError as exc:
        # A broken arc file is an operator problem, not a player-facing one.
        logs.error("engine.arc_failed", player_id=player.player_id, error=str(exc))
        return dispatch.send_to_player(
            player,
            safety.SAFE_FALLBACK,
            kind=MessageKind.SYSTEM_REPLY,
            source="engine:fallback",
        )

    direction = None
    if described.get("has_photo"):
        direction = (
            "The player just sent a photograph. You cannot see it. Respond to "
            "the fact that they sent one, and ask about it rather than "
            "pretending to have looked."
        )
    elif described.get("has_location"):
        direction = (
            "The player just shared their location. You are not told where it "
            "is and must not guess. Acknowledge it without using it."
        )

    logs.info(
        "engine.respond",
        player_id=player.player_id,
        arc_id=ctx.arc.arc_id,
        beat_id=ctx.beat.beat_id,
        day=ctx.day,
        history=len(ctx.history),
    )
    return _send(player, ctx, _generate(player, ctx, direction), source="engine")


def open_beat(player: Player, *, now=None) -> SendResult:
    """Send the message that opens the player's current beat.

    Nothing calls this on a schedule yet -- that is the next phase. It exists
    now because it is how the arc actually moves: without it the story can only
    ever react, and a seven-day arc that never speaks first is not the product.
    """
    ctx = build_context(player, now=now)
    direction = (
        f"Open day {ctx.day}. {ctx.beat.opens_with or ctx.beat.goal} "
        "The player has not said anything since your last message, so start "
        "the day yourself."
    )
    logs.info(
        "engine.open_beat",
        player_id=player.player_id,
        beat_id=ctx.beat.beat_id,
        day=ctx.day,
    )
    result = _send(player, ctx, _generate(player, ctx, direction), source="engine:open")
    if result.sent:
        store.record_beat(player.player_id, ctx.beat.beat_id, opened_at=iso(now_utc()))
    return result
