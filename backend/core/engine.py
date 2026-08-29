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

from backend.core import (
    config,
    dispatch,
    geo,
    logs,
    model,
    review,
    safety,
    store,
    vision,
)
from backend.core.clock import arc_day, iso, now_utc, parse_iso, to_local
from backend.core.models import (
    Channel,
    Direction,
    EventKind,
    MessageKind,
    Player,
    SendResult,
)
from backend.story.arc import Advance, Arc, ArcError, Beat, load_arc, parse_arc

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
    """This player's arc: the one composed for them, or the shipped default.

    A generated walking arc is built around where the player actually is, so it
    exists only for them and lives in DynamoDB. The stored one always wins --
    otherwise a player who has been given a bespoke adventure would quietly be
    handed the stock seven-day story instead.
    """
    stored = store.get_arc(player.player_id)
    if stored:
        return parse_arc(stored, source=f"stored:{player.player_id}")
    return load_arc(player.arc_id or config.DEFAULT_ARC_ID)


def build_context(player: Player, *, now=None) -> Context:
    """Everything the model is allowed to know about this player's story.

    Where the player is in the arc comes from two different places, and neither
    is the model. A ``days`` arc derives it from the calendar; a ``stages`` arc
    reads a pointer that only moves when something verifiable happened.
    """
    now = now or now_utc()
    arc = arc_for(player)
    started = parse_iso(player.arc_started_at) if player.arc_started_at else now
    day = arc_day(started, now, player.timezone)
    beat = arc.beat_at(player.beat_order) if arc.is_walk else arc.beat_at(day)
    return Context(arc=arc, beat=beat, day=day, history=_history(player))


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


#: Rules that hold whatever kind of arc is running.
_UNIVERSAL_RULES = """\
- Never claim to be a real person, deny being AI, or say the story is real.
- Never present anything as a real emergency in the player's own life, and
  never tell them to call anyone or seek help for a real situation.
- Never claim to be police, a bank, a hospital, a government body, an
  employer, or any other real institution.
- Never ask for money, payment, passwords, codes, documents, or account
  details.
- If the player sounds frightened, distressed, or sincerely asks whether this
  is real, stop the story immediately. Drop out of character, say plainly that
  it is a written story and that they can send STOP to end it, and do not
  return to the fiction in that message."""

#: A sit-down arc. The player is wherever they already are and stays there.
_ARMCHAIR_RULES = """\
- Never ask the player to go anywhere, enter anywhere, travel, drive, or
  approach, follow or photograph a person. Asking them to describe or
  photograph an ordinary object where they already are is fine."""

#: A walking arc. Sending the player out is the whole point, so the rule is not
#: "never move them" but "only ever to a public place, in daylight, at their
#: own pace, and never near anybody".
_WALKING_RULES = """\
This is a walking adventure: sending the player to a real place is the point.
What you may not do is send them anywhere unsafe, or make going feel urgent.

- Only the named places in the route. Never invent a destination, never give a
  street address, and never write a coordinate -- you have not been given any.
- Outdoors and public, in daylight. Never inside a building, never onto
  private property, never over a fence, and never anywhere that would need a
  ticket or a fee.
- Never send them toward a person. No approaching, following, waiting for or
  watching anybody. Other people in the street are scenery.
- Photographs are of things: a statue, a doorway, a sign, a view. Never of a
  person, and never of anything that would mean pointing a camera at a
  stranger.
- No urgency. No countdowns, no deadlines, nobody following them, nothing bad
  if they are slow. They may stop for an hour or go home; say so warmly if
  they seem tired.
- Never suggest they hurry across a road, and never comment on traffic as
  though you can see it. You cannot see them.
- If they say they cannot reach a place, or do not want to, that is fine and
  final. Offer to skip it. Never ask twice."""


def _system_prompt(player: Player, ctx: Context, *, now=None) -> str:
    now = now or now_utc()
    local = to_local(now, player.timezone)
    used = store.quota_used(player.player_id, player.timezone)
    remaining = max(0, config.MAX_STORY_MESSAGES_PER_DAY - used)
    walk = ctx.arc.is_walk

    where = (
        f"""# The route

The whole walk, in order. The player is on step {ctx.beat.order} of
{ctx.arc.length}.

{ctx.arc.route}

# Right now

{ctx.beat.render()}"""
        if walk
        else f"""# Today

{ctx.beat.render()}"""
    )

    pacing = (
        f"""{player.display_name}. Their local time is {local:%A %H:%M}. They are on
step {ctx.beat.order} of {ctx.arc.length} of a walk they are doing today. You
have {remaining} of today's {config.MAX_STORY_MESSAGES_PER_DAY} messages left,
so do not spend them on chatter -- they are out on their feet, and a message
they have to stop and read had better be worth stopping for."""
        if walk
        else f"""\
{player.display_name}. Their local time is {local:%A %H:%M}. This is day
{ctx.day} of {ctx.arc.duration_days}. You have {remaining} of today's
{config.MAX_STORY_MESSAGES_PER_DAY} messages left, so pace the day
accordingly; if this is the last one, do not leave a question hanging on an
answer that cannot arrive until tomorrow."""
    )

    return f"""You are writing an interactive fiction story delivered over Telegram.

# The story

{ctx.arc.premise}

# Tone

{ctx.arc.tone}

# Cast

{ctx.arc.cast}

{where}

# Who you are writing to

{pacing}

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

{_UNIVERSAL_RULES}
{_WALKING_RULES if walk else _ARMCHAIR_RULES}

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

    verdict = review.review_outbound(
        candidate, recent=ctx.transcript, walking=ctx.arc.is_walk
    )
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


def check_arrival(beat: Beat, coordinates: tuple[float, float] | None) -> dict | None:
    """Compare a position against this beat's fence, then forget the position.

    Returns the lossy summary from :func:`geo.describe_progress` -- arrived,
    a distance band, a compass direction -- and nothing else. The coordinates
    do not leave this function, which is the whole reason it exists as a
    separate step rather than being inlined into the handler.
    """
    if coordinates is None or beat.target is None:
        return None
    try:
        here = geo.Point(*coordinates)
    except ValueError as exc:  # a malformed update, not a player problem
        logs.warn("engine.bad_coordinates", error=str(exc))
        return None
    return geo.describe_progress(here, beat.target, beat.fence_m)


def _advances(
    beat: Beat,
    described: dict,
    progress: dict | None,
    look: vision.Look | None = None,
) -> bool:
    """Has the player done the thing this beat is waiting for?

    Arrival is a fact and nothing here asks a model about it -- being told
    "yes, they're there" while somebody is standing in the wrong street is the
    failure that matters.

    A photograph is the one case that needs judgement, because "a file
    arrived" is not the same as "they photographed something". It is
    deliberately generous: a model cannot know what a particular local
    sculpture looks like, so it only has to be plausibly a picture of the kind
    of thing they were sent for. When nobody could look at it, the photo
    counts -- a player who did as they were asked must not be stranded by a
    timeout.
    """
    match beat.advance_on:
        case Advance.ARRIVAL:
            return bool(progress and progress["arrived"])
        case Advance.PHOTO:
            if not described.get("has_photo"):
                return False
            return look is None or look.unavailable or look.plausible
        case Advance.REPLY:
            return True
        case _:
            # OPERATOR and DAY are not something the player can trigger.
            return False


def _walk_direction(
    ctx: Context,
    described: dict,
    progress: dict | None,
    look: vision.Look | None = None,
) -> str:
    """What to tell the writer about a player who has not got there yet."""
    beat = ctx.beat
    if look is not None and not look.plausible and not look.unavailable:
        # They sent something, but not a photograph of anything they were sent
        # for. Say so kindly; it is almost always a confused player.
        return (
            f"They sent a photograph, but it does not show anything like "
            f"{beat.waypoint.name if beat.waypoint else 'the place'} -- "
            f"what is actually in it: {look.description or 'unclear'}. "
            "Do not accept it as the picture you asked for and do not pretend "
            "to see what is not there. Be light about it and ask again."
        )
    if beat.advance_on is Advance.ARRIVAL and progress:
        return (
            f"They sent their location and they are not at {beat.waypoint.name} "
            f"yet -- it is {progress['distance_band']} to the "
            f"{progress['direction']} of them. Keep them company and nudge them "
            "on. Do not give a distance in metres or a direction as a bearing; "
            "you only have a rough sense of it."
        )
    if beat.advance_on is Advance.ARRIVAL:
        return (
            f"They are still on their way to {beat.waypoint.name} and have not "
            "shared their location, so you do not know where they are. Do not "
            "guess, and do not ask them to share it -- Telegram's attachment "
            "menu is how, if they want to. Talk to them about what they said."
        )
    if beat.advance_on is Advance.PHOTO and not described.get("has_photo"):  # noqa: SIM102
        return (
            f"They have not sent the photograph from {beat.waypoint.name} yet. "
            "Stay in the moment with them; ask for it once, lightly, and only "
            "if it fits what they just said."
        )
    if described.get("has_photo"):
        return (
            "They sent a photograph. You cannot see it. Respond to the fact of "
            "it and ask about what is in it rather than pretending to have "
            "looked."
        )
    return ""


def _photo_direction(look: vision.Look | None, place: str) -> str:
    """What the writer is allowed to say about a photograph.

    The rule it exists to enforce: **describe only what you were told is
    there.** Without it the character invents detail -- "there is a marking
    near the bottom edge, is there not?" about an image it never saw -- and
    the one person who can check is the person holding the photograph.
    """
    if look is None or look.unavailable or not look.description:
        return (
            f"They sent a photograph from {place}. You could not see it. Say so "
            "lightly and ask them what is in it. Do not describe it, do not "
            "guess at any detail, and do not imply you looked."
        )

    warning = (
        " Somebody is visible in the frame; do not describe them, mention them, "
        "or draw attention to them."
        if look.people_visible
        else ""
    )
    return (
        f"They sent a photograph from {place}. Here is exactly what is in it, "
        f"from someone who looked: {look.description}{warning} React to what is "
        "actually described there and nothing else. Do not add details that are "
        "not in that description -- no inscriptions, marks, textures or figures "
        "that were not mentioned. If the description is vague, say what you can "
        "see and ask about the rest."
    )


def _arrival_direction(
    ctx: Context, previous: Beat, look: vision.Look | None = None
) -> str:
    """What to tell the writer when the player has just completed a stage."""
    if previous.advance_on is Advance.PHOTO:
        done = _photo_direction(
            look, previous.waypoint.name if previous.waypoint else "the last stop"
        )
    elif previous.waypoint:
        done = f"They have just reached {previous.waypoint.name}."
    else:
        done = "They have just finished the last step."

    if ctx.beat.order == previous.order:
        # The last stage: there is nowhere to send them next.
        return (
            f"{done} This is the end of the walk. Close the story warmly, thank "
            "them, and then step out of character and say plainly that it was a "
            "written story and it is finished. Leave nothing dangling."
        )
    return (
        f"{done} Pay it off -- tell them what they came there for -- and then "
        f"send them on to the next stop. {ctx.beat.goal} "
        f"Point them there like this: {ctx.beat.hint}"
    )


def _look_at_photo(beat: Beat, described: dict) -> vision.Look | None:
    """Fetch the photograph and describe it. Never raises into the turn."""
    file_id = described.get("photo_file_id")
    if not file_id:
        return None
    try:
        image = dispatch.telegram().download(file_id)
    except Exception as exc:
        logs.warn("engine.photo_download_failed", error=f"{type(exc).__name__}: {exc}")
        return None
    return vision.look(
        image, target=beat.waypoint.name if beat.waypoint else "the stop"
    )


def _walk(
    player: Player,
    ctx: Context,
    described: dict,
    coordinates: tuple[float, float] | None,
    progress: dict | None,
) -> SendResult:
    """One turn of a walking arc."""
    beat = ctx.beat

    # Look at the photograph before deciding anything. A picture of a desk
    # should not satisfy a stop, and the writer cannot honestly react to an
    # image nobody has described to it.
    look = (
        _look_at_photo(beat, described)
        if beat.advance_on is Advance.PHOTO and described.get("has_photo")
        else None
    )
    if look is not None:
        store.record_event(
            player.player_id,
            direction=Direction.IN,
            channel=Channel.TELEGRAM,
            kind=EventKind.PHOTO,
            text=look.description or None,
            beat_id=beat.beat_id,
            waypoint=beat.waypoint.name if beat.waypoint else None,
            plausible=look.plausible,
            people_visible=look.people_visible or None,
            # A photograph of something unrelated is worth an operator's eyes:
            # it is usually a confused player, occasionally a bored one.
            needs_review=(not look.plausible) or None,
            source="vision",
        )

    if not _advances(beat, described, progress, look):
        logs.info(
            "engine.walk_waiting",
            player_id=player.player_id,
            beat_id=beat.beat_id,
            advance_on=str(beat.advance_on),
            arrived=bool(progress and progress["arrived"]),
            photo_plausible=look.plausible if look else None,
        )
        direction = _walk_direction(ctx, described, progress, look)
        return _send(player, ctx, _generate(player, ctx, direction), source="engine")

    # --- the stage is complete -------------------------------------------
    store.record_beat(
        player.player_id,
        beat.beat_id,
        title=beat.title,
        completed_at=iso(now_utc()),
        completed_by=str(beat.advance_on),
    )
    next_order = min(beat.order + 1, ctx.arc.length)
    if next_order != beat.order:
        store.set_beat_order(player.player_id, next_order)
        player.beat_order = next_order

    logs.info(
        "engine.stage_complete",
        player_id=player.player_id,
        beat_id=beat.beat_id,
        completed_by=str(beat.advance_on),
        next_beat_order=next_order,
    )
    moved = Context(
        arc=ctx.arc, beat=ctx.arc.beat_at(next_order), day=ctx.day, history=ctx.history
    )
    direction = _arrival_direction(moved, beat, look)
    return _send(player, moved, _generate(player, moved, direction), source="engine")


def respond(
    player: Player,
    described: dict,
    *,
    now=None,
    coordinates: tuple[float, float] | None = None,
) -> SendResult:
    """Answer something the player just sent. The engine's main entry point.

    ``coordinates`` are passed separately from ``described`` on purpose: that
    dict is what gets written to the timeline, and it must never carry a
    position. These are compared against a geofence and dropped.
    """
    try:
        ctx = build_context(player, now=now)
    except ArcError as exc:
        # A broken arc is an operator problem, not a player-facing one.
        logs.error("engine.arc_failed", player_id=player.player_id, error=str(exc))
        return dispatch.send_to_player(
            player,
            safety.SAFE_FALLBACK,
            kind=MessageKind.SYSTEM_REPLY,
            source="engine:fallback",
        )

    logs.info(
        "engine.respond",
        player_id=player.player_id,
        arc_id=ctx.arc.arc_id,
        kind=str(ctx.arc.kind),
        beat_id=ctx.beat.beat_id,
        day=ctx.day,
        history=len(ctx.history),
    )

    # A shared location is recorded here, once, for either kind of arc -- the
    # handler deliberately does not, because what belongs on the timeline is
    # the *result* of the fence check, and only this side has run it. The
    # coordinates end their life inside `check_arrival`.
    progress = check_arrival(ctx.beat, coordinates)
    if described.get("has_location"):
        store.record_event(
            player.player_id,
            direction=Direction.IN,
            channel=Channel.TELEGRAM,
            kind=EventKind.LOCATION,
            text=None,
            beat_id=ctx.beat.beat_id,
            waypoint=ctx.beat.waypoint.name if ctx.beat.waypoint else None,
            arrived=bool(progress and progress["arrived"]),
            distance_band=(progress or {}).get("distance_band"),
            source="player",
        )

    if ctx.arc.is_walk:
        return _walk(player, ctx, described, coordinates, progress)

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

    return _send(player, ctx, _generate(player, ctx, direction), source="engine")


def open_beat(player: Player, *, now=None) -> SendResult:
    """Send the message that opens the player's current beat.

    Nothing calls this on a schedule yet -- that is the next phase. It exists
    now because it is how the arc actually moves: without it the story can only
    ever react, and a seven-day arc that never speaks first is not the product.
    """
    ctx = build_context(player, now=now)
    opening = ctx.beat.opens_with or ctx.beat.goal
    if ctx.arc.is_walk:
        direction = (
            f"Open the adventure at step {ctx.beat.order}. {opening} "
            f"Point them toward {ctx.beat.waypoint.name}: {ctx.beat.hint}"
            if ctx.beat.waypoint
            else f"Open step {ctx.beat.order}. {opening}"
        )
    else:
        direction = (
            f"Open day {ctx.day}. {opening} "
            "The player has not said anything since your last message, so "
            "start the day yourself."
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
