"""Arc definitions: the story as data, not as code.

There are two shapes of arc, and the difference is only *what makes the story
move on*:

``days``
    One beat per calendar day, as in the seven-day ``nightjar`` arc. The
    current beat is arithmetic over player-local days -- see
    :func:`backend.core.clock.arc_day`.

``stages``
    One beat per place, for a single-day walking adventure. The current beat is
    a stored pointer that only moves when the player does something the system
    can *verify*: standing inside a geofence, or sending a photograph.

Neither kind ever asks the model how the story is going. A model that can talk
itself into "the arc is finished" is a model that can end someone's day early,
and one that can talk itself into "they've arrived" is worse -- it would move
the story on while the player is still standing in the wrong street.

A ``days`` arc is a static YAML file in ``arcs/``. A ``stages`` arc is
generated per player from where they actually are, so it lives in DynamoDB
rather than in the repo; both go through :func:`parse_arc`, and both are
validated the same way.

Nothing in an arc is a safety control. The rules in
:mod:`backend.core.safety` and :mod:`backend.core.validation` apply whatever an
arc file says, and an arc asking for something forbidden would simply have its
messages refused.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from backend.core.geo import DEFAULT_FENCE_M, Point, clamp_fence
from backend.core.places import Waypoint

ARCS_DIR = Path(__file__).parent / "arcs"


class ArcError(ValueError):
    """An arc is missing, unreadable, or internally inconsistent."""


class ArcKind(StrEnum):
    DAYS = "days"
    STAGES = "stages"


class Advance(StrEnum):
    """What moves the story past a beat.

    Every value except :attr:`REPLY` is verified by the system rather than
    judged: a day has rolled over, a coordinate is inside a fence, a photo
    arrived, or a human pressed the button.
    """

    #: The player-local calendar day changed. ``days`` arcs only.
    DAY = "day"
    #: The player's location was inside this beat's geofence.
    ARRIVAL = "arrival"
    #: The player sent a photograph while on this beat.
    PHOTO = "photo"
    #: Any reply at all. For talky beats with nowhere to walk to.
    REPLY = "reply"
    #: Only the operator, from the console. Used for the final beat.
    OPERATOR = "operator"


@dataclass(frozen=True)
class Character:
    name: str
    role: str
    voice: str

    def render(self) -> str:
        return f"{self.name} -- {self.role}. Voice: {self.voice}"


@dataclass(frozen=True)
class Beat:
    """One step of the arc: a day in a ``days`` arc, a place in a ``stages`` one."""

    beat_id: str
    #: 1-based position. In a ``days`` arc this *is* the day number.
    order: int
    title: str
    #: What this beat is for, addressed to the writer.
    goal: str
    advance_on: Advance = Advance.REPLY
    #: The proactive message that opens the beat, as direction rather than copy.
    opens_with: str = ""
    player_may: str = ""
    channel: str = "telegram"
    #: Where the player is being sent. ``stages`` arcs only.
    waypoint: Waypoint | None = None
    #: How the character points them there, in words, without coordinates.
    hint: str = ""

    @property
    def day(self) -> int:
        """Alias kept for ``days`` arcs, where order and day are the same thing."""
        return self.order

    @property
    def fence_m(self) -> float:
        return clamp_fence(self.waypoint.fence_m if self.waypoint else DEFAULT_FENCE_M)

    @property
    def target(self) -> Point | None:
        return self.waypoint.point if self.waypoint else None

    @property
    def requirement(self) -> str:
        """What the player must actually do here, spelled out for the writer.

        Without this the writer asks for whatever suits the sentence -- a photo
        at a stage that is waiting for an arrival, say -- and the player does
        it, nothing happens, and they are stuck being asked again for something
        that was never the gate.
        """
        match self.advance_on:
            case Advance.ARRIVAL:
                return (
                    "This step ends ONLY when they share their location from "
                    "the place. Ask them for it in your own words, and say how: "
                    "the paperclip or attachment button in Telegram, then "
                    "Location. Do not ask for a photograph here -- a photo will "
                    "not move the story on and they will think they are stuck."
                )
            case Advance.PHOTO:
                return (
                    "This step ends ONLY when they send a photograph from the "
                    "place. Ask for the picture. Do not ask them to share their "
                    "location here -- it will not move the story on."
                )
            case Advance.OPERATOR:
                return (
                    "This is the last step. Nothing they send ends it, so ask "
                    "for nothing they have to go and do."
                )
            case _:
                return "This step moves on when they reply."

    def render(self) -> str:
        lines = [f"Step {self.order} -- {self.title}", f"Purpose: {self.goal}"]
        if self.waypoint:
            lines.append(f"Where they are going: {self.waypoint.render()}")
            if self.hint:
                lines.append(f"How to point them there: {self.hint}")
            lines.append(f"How this step ends: {self.requirement}")
        if self.player_may:
            lines.append(f"The player may: {self.player_may}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Arc:
    arc_id: str
    title: str
    premise: str
    tone: str
    characters: tuple[Character, ...]
    beats: tuple[Beat, ...]
    kind: ArcKind = ArcKind.DAYS
    #: Kept as a field rather than derived from len(beats) so a malformed arc
    #: fails validation instead of silently running short.
    duration_days: int = 7
    notes: str = field(default="")

    @property
    def is_walk(self) -> bool:
        return self.kind is ArcKind.STAGES

    @property
    def length(self) -> int:
        return len(self.beats)

    def beat_at(self, order: int) -> Beat:
        """The beat at a 1-based position, clamped to the arc.

        Clamping rather than raising is deliberate: a player who goes quiet for
        a fortnight and then answers should get the last beat, not an error.
        """
        clamped = max(1, min(order, self.length))
        for beat in self.beats:
            if beat.order == clamped:
                return beat
        return self.beats[-1]

    #: Kept as the name the day-based path reads, so that call site says what
    #: it means rather than passing a day into something called `beat_at`.
    beat_for_day = beat_at

    def beat(self, beat_id: str) -> Beat | None:
        return next((b for b in self.beats if b.beat_id == beat_id), None)

    def index_of(self, beat_id: str) -> int | None:
        beat = self.beat(beat_id)
        return beat.order if beat else None

    @property
    def cast(self) -> str:
        return "\n".join(c.render() for c in self.characters)

    @property
    def route(self) -> str:
        """The walk, in order, for a prompt. Names only -- never coordinates."""
        return "\n".join(
            f"{b.order}. {b.waypoint.render()}" for b in self.beats if b.waypoint
        )

    def to_item(self) -> dict[str, Any]:
        """Serialise for DynamoDB. Round-trips through :func:`parse_arc`."""
        return {
            "arc_id": self.arc_id,
            "kind": str(self.kind),
            "title": self.title,
            "premise": self.premise,
            "tone": self.tone,
            "duration_days": self.duration_days,
            "notes": self.notes,
            "characters": [
                {"name": c.name, "role": c.role, "voice": c.voice}
                for c in self.characters
            ],
            "beats": [
                {
                    "beat_id": b.beat_id,
                    "order": b.order,
                    "title": b.title,
                    "goal": b.goal,
                    "advance_on": str(b.advance_on),
                    "opens_with": b.opens_with,
                    "player_may": b.player_may,
                    "channel": b.channel,
                    "hint": b.hint,
                    "waypoint": b.waypoint.to_item() if b.waypoint else None,
                }
                for b in self.beats
            ],
        }


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data or data[key] in (None, ""):
        raise ArcError(f"{where}: missing required key {key!r}")
    return data[key]


def _parse_waypoint(data: dict[str, Any] | None, where: str) -> Waypoint | None:
    if not data:
        return None
    try:
        return Waypoint(
            osm_id=str(data.get("osm_id") or "unknown"),
            name=_require(data, "name", where),
            kind=str(data.get("kind") or "place"),
            lat=float(_require(data, "lat", where)),
            lon=float(_require(data, "lon", where)),
            fence_m=int(data.get("fence_m") or DEFAULT_FENCE_M),
        )
    except (TypeError, ValueError) as exc:
        raise ArcError(f"{where}: unusable waypoint -- {exc}") from exc


def parse_arc(data: dict[str, Any], *, source: str = "<dict>") -> Arc:
    arc_id = _require(data, "arc_id", source)
    kind = ArcKind(str(data.get("kind") or ArcKind.DAYS))

    characters = tuple(
        Character(
            name=_require(c, "name", f"{source}: character"),
            role=_require(c, "role", f"{source}: character"),
            voice=_require(c, "voice", f"{source}: character"),
        )
        for c in data.get("characters") or []
    )
    if not characters:
        raise ArcError(f"{source}: arc has no characters")

    beats = []
    for raw in data.get("beats") or []:
        where = f"{source}: beat"
        # `day` reads better than `order` in a hand-written seven-day arc, and
        # in that arc they are the same number.
        order = raw.get("order", raw.get("day"))
        if order in (None, ""):
            raise ArcError(f"{where}: missing required key 'order'")
        try:
            advance = Advance(
                str(
                    raw.get("advance_on")
                    or (Advance.DAY if kind is ArcKind.DAYS else Advance.REPLY)
                )
            )
        except ValueError as exc:
            raise ArcError(
                f"{where}: unknown advance_on {raw.get('advance_on')!r}"
            ) from exc

        beats.append(
            Beat(
                beat_id=_require(raw, "beat_id", where),
                order=int(order),
                title=_require(raw, "title", where),
                goal=_require(raw, "goal", where),
                advance_on=advance,
                opens_with=(raw.get("opens_with") or "").strip(),
                player_may=(raw.get("player_may") or "").strip(),
                channel=(raw.get("channel") or "telegram").strip(),
                hint=(raw.get("hint") or "").strip(),
                waypoint=_parse_waypoint(raw.get("waypoint"), where),
            )
        )
    if not beats:
        raise ArcError(f"{source}: arc has no beats")

    default_days = len(beats) if kind is ArcKind.DAYS else 1
    arc = Arc(
        arc_id=arc_id,
        title=_require(data, "title", source),
        premise=_require(data, "premise", source).strip(),
        tone=_require(data, "tone", source).strip(),
        characters=characters,
        beats=tuple(beats),
        kind=kind,
        duration_days=int(data.get("duration_days") or default_days),
        notes=(data.get("notes") or "").strip(),
    )
    _validate(arc, source)
    return arc


def _validate(arc: Arc, source: str) -> None:
    """Catch the mistakes that would otherwise surface in front of a player."""
    ids = [b.beat_id for b in arc.beats]
    if len(set(ids)) != len(ids):
        raise ArcError(f"{source}: duplicate beat_id")

    orders = [b.order for b in arc.beats]
    if orders != sorted(orders):
        raise ArcError(f"{source}: beats are out of order: {orders}")
    if orders != list(range(1, len(orders) + 1)):
        raise ArcError(f"{source}: beats must be numbered 1..n, got {orders}")

    if arc.kind is ArcKind.DAYS:
        if orders != list(range(1, arc.duration_days + 1)):
            raise ArcError(
                f"{source}: arc runs {arc.duration_days} days "
                f"but has beats for {orders}"
            )
        return

    # --- stages ---------------------------------------------------------
    if arc.duration_days != 1:
        raise ArcError(f"{source}: a stages arc is one day, not {arc.duration_days}")

    for beat in arc.beats:
        if beat.advance_on is Advance.DAY:
            raise ArcError(
                f"{source}: beat {beat.beat_id!r} advances on 'day' in a stages arc"
            )
        if beat.advance_on is Advance.ARRIVAL and beat.waypoint is None:
            raise ArcError(
                f"{source}: beat {beat.beat_id!r} waits for an arrival "
                "but has no waypoint to arrive at"
            )

    if arc.beats[-1].advance_on is not Advance.OPERATOR:
        # Otherwise the last beat "completes" and the player is left on an arc
        # that has run out, with nothing to say and no one watching.
        raise ArcError(
            f"{source}: the last beat must advance_on 'operator', "
            f"not {arc.beats[-1].advance_on!r}"
        )


@functools.lru_cache(maxsize=8)
def load_arc(arc_id: str) -> Arc:
    """Read and validate one arc file. Cached -- these are static."""
    path = ARCS_DIR / f"{arc_id}.yaml"
    if not path.is_file():
        raise ArcError(f"no arc named {arc_id!r} in {ARCS_DIR}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ArcError(f"{path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ArcError(f"{path.name}: expected a mapping at the top level")
    return parse_arc(data, source=path.name)


def list_arcs() -> list[str]:
    return sorted(p.stem for p in ARCS_DIR.glob("*.yaml"))
