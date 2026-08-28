"""Arc definitions: the story as data, not as code.

An arc is a YAML file in ``backend/story/arcs/``. It carries the premise, the
characters and their voices, and one beat per day of the run. Nothing in it is
executable and nothing in it is a safety control -- the rules in
:mod:`backend.core.safety` and :mod:`backend.core.validation` apply to every
arc regardless of what its text says, and an arc that asked for something
forbidden would simply have its messages refused.

**The current beat is a function of elapsed days, not a model judgement.**
Asking the model "is this beat finished?" makes the story's shape depend on
the thing least able to be held to it: a stuck beat repeats forever and a
runaway one burns the whole arc in an afternoon. Days are the one axis the
player and the operator both already understand, and the same arithmetic tells
the scheduler which beat to open tomorrow morning.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ARCS_DIR = Path(__file__).parent / "arcs"


class ArcError(ValueError):
    """An arc file is missing, unreadable, or internally inconsistent."""


@dataclass(frozen=True)
class Character:
    name: str
    role: str
    voice: str

    def render(self) -> str:
        return f"{self.name} -- {self.role}. Voice: {self.voice}"


@dataclass(frozen=True)
class Beat:
    """One day of the arc."""

    beat_id: str
    day: int
    title: str
    #: What this day is for, in the second person, addressed to the writer.
    goal: str
    #: The proactive message that opens the day. Written as direction, not as
    #: copy to send verbatim -- the model still writes the actual words.
    opens_with: str = ""
    #: What the player is likely to do, so the writer can anticipate it.
    player_may: str = ""
    #: Which channel this beat's proactive contact uses.
    channel: str = "telegram"

    def render(self) -> str:
        lines = [f"Day {self.day} -- {self.title}", f"Purpose: {self.goal}"]
        if self.player_may:
            lines.append(f"The player may: {self.player_may}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Arc:
    arc_id: str
    title: str
    premise: str
    #: How the story addresses the player and what it must never claim.
    tone: str
    characters: tuple[Character, ...]
    beats: tuple[Beat, ...]
    #: Kept as a field rather than derived from len(beats) so a malformed file
    #: fails validation instead of silently running a shorter arc.
    duration_days: int = 7
    notes: str = field(default="")

    def beat_for_day(self, day: int) -> Beat:
        """The beat for a given 1-based day, clamped to the arc's range.

        Clamping rather than raising is deliberate: a player who goes quiet for
        a fortnight and then answers should get the last beat, not an error.
        """
        clamped = max(1, min(day, self.duration_days))
        for beat in self.beats:
            if beat.day == clamped:
                return beat
        return self.beats[-1]

    def beat(self, beat_id: str) -> Beat | None:
        return next((b for b in self.beats if b.beat_id == beat_id), None)

    @property
    def cast(self) -> str:
        return "\n".join(c.render() for c in self.characters)


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data or data[key] in (None, ""):
        raise ArcError(f"{where}: missing required key {key!r}")
    return data[key]


def parse_arc(data: dict[str, Any], *, source: str = "<dict>") -> Arc:
    arc_id = _require(data, "arc_id", source)

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

    beats = tuple(
        Beat(
            beat_id=_require(b, "beat_id", f"{source}: beat"),
            day=int(_require(b, "day", f"{source}: beat")),
            title=_require(b, "title", f"{source}: beat"),
            goal=_require(b, "goal", f"{source}: beat"),
            opens_with=(b.get("opens_with") or "").strip(),
            player_may=(b.get("player_may") or "").strip(),
            channel=(b.get("channel") or "telegram").strip(),
        )
        for b in data.get("beats") or []
    )
    if not beats:
        raise ArcError(f"{source}: arc has no beats")

    duration = int(data.get("duration_days") or len(beats))

    arc = Arc(
        arc_id=arc_id,
        title=_require(data, "title", source),
        premise=_require(data, "premise", source).strip(),
        tone=_require(data, "tone", source).strip(),
        characters=characters,
        beats=beats,
        duration_days=duration,
        notes=(data.get("notes") or "").strip(),
    )
    _validate(arc, source)
    return arc


def _validate(arc: Arc, source: str) -> None:
    """Catch the mistakes that would otherwise surface mid-run, in front of a player."""
    ids = [b.beat_id for b in arc.beats]
    if len(set(ids)) != len(ids):
        raise ArcError(f"{source}: duplicate beat_id")

    days = [b.day for b in arc.beats]
    if days != sorted(days):
        raise ArcError(f"{source}: beats are not in day order: {days}")
    if len(set(days)) != len(days):
        raise ArcError(f"{source}: two beats share a day: {days}")

    expected = list(range(1, arc.duration_days + 1))
    if days != expected:
        raise ArcError(
            f"{source}: arc runs {arc.duration_days} days but has beats for {days}"
        )


@functools.lru_cache(maxsize=8)
def load_arc(arc_id: str) -> Arc:
    """Read and validate one arc. Cached -- arcs are static files."""
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
