"""Compose a one-day walking arc around wherever the player actually is.

The customer gives a location at onboarding; this turns it into a story with
real destinations. Three steps: ask OpenStreetMap what is nearby, have the
model choose a route through those places and write a character around it,
then check the result is something a person can actually walk.

**The model never supplies a coordinate.** It is shown a numbered list of real
places and picks by number; the coordinates are looked back up here, from the
Overpass results. A model that could invent a location would eventually invent
one that does not exist, or one across a motorway, and the player would be the
one to find out.

The structural rules are applied here rather than asked for. Which beat waits
on an arrival, and the fact that the last beat only ever ends when the operator
says so, are set in code after the model has written its part -- so a generated
arc cannot be structurally invalid however the writing goes.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.core import config, logs, model
from backend.core.geo import Point, distance_m
from backend.core.places import PlacesUnavailable, Waypoint, find_nearby
from backend.story.arc import Advance, Arc, ArcError, ArcKind, parse_arc

#: Fewest places worth calling an adventure, and the most before it becomes a
#: chore. Four to six stops is a comfortable couple of hours on foot.
MIN_STAGES = 3
MAX_STAGES = 6

#: A route longer than this stops being a walk. Checked after generation
#: because the model is choosing the order, and it cannot do the arithmetic.
MAX_ROUTE_M = 4500
#: No single leg should be a trek in itself.
MAX_LEG_M = 1400

#: Overpass needs enough candidates that the model has real choices, but a
#: wider search pulls in places too far to walk between.
SEARCH_RADIUS_M = 1400
CANDIDATE_LIMIT = 20


class ArcGenerationFailed(RuntimeError):
    """No usable arc could be composed. The operator sees this, never a player."""


@dataclass(frozen=True)
class Draft:
    """A generated arc plus what the operator needs to judge and edit it."""

    arc: Arc
    route_m: float
    #: Every place that was offered to the writer, not only the ones it chose.
    #: The console's editor needs these to offer alternatives for a stop, and
    #: re-querying Overpass for them would be a second call to a donated
    #: service for a list we already had.
    candidates: list[Waypoint]

    @property
    def candidates_considered(self) -> int:
        return len(self.candidates)


COMPOSE_TOOL = {
    "name": "compose_arc",
    "description": (
        "Write the one-day adventure. Call this exactly once, choosing places "
        "only from the numbered list you were given."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Two or three words. The name of the adventure.",
            },
            "premise": {
                "type": "string",
                "description": (
                    "The situation, in 3-5 sentences. Who is contacting the "
                    "player, and why they need someone out there today."
                ),
            },
            "tone": {
                "type": "string",
                "description": (
                    "How this should feel and what it must never do. Two or "
                    "three sentences."
                ),
            },
            "character": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string", "description": "Who they are."},
                    "voice": {
                        "type": "string",
                        "description": "How they write. Be specific and unusual.",
                    },
                },
                "required": ["name", "role", "voice"],
                "additionalProperties": False,
            },
            "stages": {
                "type": "array",
                "description": (
                    "The walk, in the order the player does it. Order them so "
                    "each leg is a short walk from the one before."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "place_number": {
                            "type": "integer",
                            "description": (
                                "Which numbered place from the list this stage "
                                "sends the player to."
                            ),
                        },
                        "title": {"type": "string"},
                        "goal": {
                            "type": "string",
                            "description": (
                                "What happens here and why it matters to the "
                                "story, addressed to the writer."
                            ),
                        },
                        "hint": {
                            "type": "string",
                            "description": (
                                "How the character points the player there, in "
                                "words. Name the place. Never give coordinates "
                                "or a street address."
                            ),
                        },
                        "task": {
                            "type": "string",
                            "enum": ["arrive", "photograph"],
                            "description": (
                                "'arrive' if reaching the place is enough. "
                                "'photograph' if the player should send a "
                                "picture of something there -- an object or a "
                                "view, never a person."
                            ),
                        },
                    },
                    "required": ["place_number", "title", "goal", "hint", "task"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["title", "premise", "tone", "character", "stages"],
        "additionalProperties": False,
    },
}


SYSTEM = f"""You write short location-based adventures for real people.

The player has agreed to spend a few hours walking around their own
neighbourhood following a story told over Telegram. They know from the start
that it is fiction and that the character writing to them is AI. Your job is
to build today's adventure out of the places that are actually near them.

# What you are given

A numbered list of real places, from OpenStreetMap, with roughly how far each
one is from the player's starting point. These are the only places that exist
for you. Pick from the list by number. Do not invent a place, do not name a
street, and do not write a coordinate -- you have not been given any.

# What to write

- Between {MIN_STAGES} and {MAX_STAGES} stages. Four or five is usually right.
- Order them as a walk. Each stage should be a short hop from the one before,
  not a trek back across town. You know roughly how far each place is from the
  start, so use that to keep the route tight.
- One character, writing to the player throughout. Give them a specific voice,
  a reason to need someone out there, and something they want.
- A reason each place matters. "Go to the fountain" is an errand. "The last
  photograph he took was from the fountain, facing away from the water" is a
  story.
- The hint should let someone find the place by name, the way a friend would
  describe it. Never a coordinate, never a full address.

# Hard rules about what you may ask a real person to do

These are not style notes. A stage that breaks one of them is thrown away.

- **Daylight, public places, standing outside.** Parks, squares, monuments,
  markets, the outside of a building. Never private property, never anywhere
  they would have to enter, climb, or pay to get into, and never anywhere that
  is only interesting after dark.
- **Never send them toward a person.** No approaching, following, watching, or
  waiting for anyone. No talking to staff or strangers as a task. The other
  people in the street are scenery, not part of the game.
- **Photographs are of things, never of people.** A statue, a doorway, a view,
  a sign. If a photograph could only be taken by pointing a camera at a
  stranger, do not ask for it.
- **No urgency, no timers, no being chased.** They set their own pace and may
  stop for an hour. Nothing in the story punishes a slow walk, and nothing
  suggests anyone is following them or that they are in danger.
- **Nothing that needs money**, and nothing that depends on a place being open.
- **Never anything that looks real.** No crimes to report, no emergencies, no
  officials, no lost children, nothing that would alarm a passer-by who read
  the message over their shoulder.

If the list of places is too thin to build a decent walk from, use fewer
stages rather than sending the player somewhere unsuitable."""


def _render_candidates(candidates: list[Waypoint]) -> str:
    return "\n".join(
        f"{i}. {w.name} -- {w.kind.replace('_', ' ')}, "
        f"about {round(w.distance_m / 50) * 50:.0f}m from the start"
        for i, w in enumerate(candidates, start=1)
    )


def _route_length(waypoints: list[Waypoint]) -> tuple[float, float]:
    """Total walk and longest single leg, in metres."""
    legs = [
        distance_m(a.point, b.point)
        for a, b in zip(waypoints, waypoints[1:], strict=False)
    ]
    return sum(legs), (max(legs) if legs else 0.0)


def check_route(waypoints: list[Waypoint]) -> tuple[float, float]:
    """Total walk and longest leg, refusing anything a person should not walk.

    Shared by generation and by the console's editor, on purpose: an operator
    swapping one stop for another can produce a route no model would have
    proposed, and the person who has to walk it cannot tell the difference.
    """
    total, longest = _route_length(waypoints)
    if total > MAX_ROUTE_M:
        raise ArcGenerationFailed(
            f"the route is {total / 1000:.1f}km of walking, over the "
            f"{MAX_ROUTE_M / 1000:.1f}km limit"
        )
    if longest > MAX_LEG_M:
        raise ArcGenerationFailed(
            f"one leg of the route is {longest:.0f}m, over the {MAX_LEG_M}m limit"
        )
    return total, longest


def compose(
    origin: Point,
    *,
    player_name: str,
    theme: str = "",
    arc_id: str | None = None,
) -> Draft:
    """Build a one-day walking arc around ``origin``.

    ``origin`` is the player's own position. It is used to search and to order
    the route, and it is not part of the returned arc -- what comes back is a
    list of public places.
    """
    try:
        candidates = find_nearby(
            origin, radius_m=SEARCH_RADIUS_M, limit=CANDIDATE_LIMIT
        )
    except PlacesUnavailable as exc:
        raise ArcGenerationFailed(f"could not look up nearby places: {exc}") from exc

    if len(candidates) < MIN_STAGES:
        raise ArcGenerationFailed(
            f"only {len(candidates)} mapped places within {SEARCH_RADIUS_M}m -- "
            "not enough to build a walk from. Try a different starting point."
        )

    prompt = (
        f"The player is called {player_name}.\n\n"
        f"Places near them:\n\n{_render_candidates(candidates)}\n\n"
        + (f"They asked for something along these lines: {theme}\n\n" if theme else "")
        + "Write the adventure. Call compose_arc once."
    )

    try:
        drafted = model.judge(
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tool=COMPOSE_TOOL,
            max_tokens=config.ARCSMITH_MAX_TOKENS,
            effort=config.ARCSMITH_EFFORT,
            timeout=config.ARCSMITH_TIMEOUT_S,
            retries=config.ARCSMITH_RETRIES,
            purpose="arcsmith",
        )
    except model.ModelUnavailable as exc:
        raise ArcGenerationFailed(f"the writer could not be reached: {exc}") from exc

    return _assemble(drafted, candidates, arc_id=arc_id)


def _assemble(
    drafted: dict, candidates: list[Waypoint], *, arc_id: str | None
) -> Draft:
    """Turn the model's choices into a validated arc, or refuse to.

    Everything structural is decided here, not by the model: which beat waits
    on what, and the rule that the last beat ends only when the operator says
    so.
    """
    stages = drafted.get("stages") or []
    if not MIN_STAGES <= len(stages) <= MAX_STAGES:
        raise ArcGenerationFailed(
            f"the writer produced {len(stages)} stages; "
            f"{MIN_STAGES}-{MAX_STAGES} is the usable range"
        )

    chosen: list[Waypoint] = []
    beats: list[dict] = []
    used: set[str] = set()

    for position, stage in enumerate(stages, start=1):
        number = stage.get("place_number")
        if not isinstance(number, int) or not 1 <= number <= len(candidates):
            # A place that is not on the list is a place that may not exist.
            raise ArcGenerationFailed(
                f"stage {position} refers to place {number!r}, which was not "
                f"among the {len(candidates)} offered"
            )
        waypoint = candidates[number - 1]
        if waypoint.osm_id in used:
            raise ArcGenerationFailed(
                f"stage {position} sends the player back to {waypoint.name!r}"
            )
        used.add(waypoint.osm_id)
        chosen.append(waypoint)

        last = position == len(stages)
        photo = str(stage.get("task") or "arrive") == "photograph"
        beats.append(
            {
                "beat_id": f"s{position}-{_slug(waypoint.name)}",
                "order": position,
                "title": stage.get("title") or waypoint.name,
                "goal": stage.get("goal") or "",
                "hint": stage.get("hint") or "",
                # The final beat never self-completes: an arc that runs out
                # leaves the player mid-street with nobody watching.
                "advance_on": str(
                    Advance.OPERATOR
                    if last
                    else (Advance.PHOTO if photo else Advance.ARRIVAL)
                ),
                "waypoint": waypoint.to_item(),
            }
        )

    total, longest = check_route(chosen)

    character = drafted.get("character") or {}
    data = {
        "arc_id": arc_id or f"walk-{_slug(drafted.get('title') or 'adventure')}",
        "kind": str(ArcKind.STAGES),
        "duration_days": 1,
        "title": drafted.get("title") or "An afternoon out",
        "premise": drafted.get("premise") or "",
        "tone": drafted.get("tone") or "",
        "characters": [
            {
                "name": character.get("name") or "the caller",
                "role": character.get("role") or "someone who needs help today",
                "voice": character.get("voice") or "plain and direct",
            }
        ],
        "beats": beats,
    }

    try:
        arc = parse_arc(data, source="generated")
    except ArcError as exc:
        raise ArcGenerationFailed(f"the composed arc did not validate: {exc}") from exc

    logs.info(
        "arcsmith.composed",
        arc_id=arc.arc_id,
        stages=arc.length,
        route_m=round(total),
        longest_leg_m=round(longest),
        candidates=len(candidates),
    )
    return Draft(arc=arc, route_m=total, candidates=list(candidates))


def _slug(text: str) -> str:
    kept = [c.lower() if c.isalnum() else "-" for c in text.strip()]
    slug = "".join(kept).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    # Non-Latin names slug away to nothing; a positional id still sorts and
    # reads correctly in a log line.
    return slug[:40] or "stage"
