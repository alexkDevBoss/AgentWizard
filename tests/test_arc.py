"""Arc files.

The shipped arc is validated here rather than discovered to be broken halfway
through someone's week, and the loader's own rules are tested from both sides.
"""

from __future__ import annotations

import pytest

from backend.core import config
from backend.story.arc import ArcError, list_arcs, load_arc, parse_arc

MINIMAL = {
    "arc_id": "test",
    "title": "Test",
    "premise": "A premise.",
    "tone": "A tone.",
    "duration_days": 2,
    "characters": [{"name": "A", "role": "r", "voice": "v"}],
    "beats": [
        {"beat_id": "d1", "day": 1, "title": "One", "goal": "g"},
        {"beat_id": "d2", "day": 2, "title": "Two", "goal": "g"},
    ],
}


def variant(**changes):
    data = {k: (v.copy() if isinstance(v, list) else v) for k, v in MINIMAL.items()}
    data.update(changes)
    return data


# ------------------------------------------------------------- the real arc


def test_the_default_arc_loads() -> None:
    arc = load_arc(config.DEFAULT_ARC_ID)
    assert arc.arc_id == config.DEFAULT_ARC_ID
    assert arc.duration_days == 7
    assert len(arc.beats) == 7


def test_every_shipped_arc_is_valid() -> None:
    """A malformed arc must fail in CI, not in front of a player."""
    assert list_arcs()
    for arc_id in list_arcs():
        load_arc(arc_id)


def test_the_default_arc_uses_the_channels_the_spec_allows() -> None:
    arc = load_arc(config.DEFAULT_ARC_ID)
    channels = [b.channel for b in arc.beats]
    assert channels.count("voice") == 1, "the spec allows exactly one call"
    assert channels.count("email") == 1, "the spec allows exactly one email"


# ------------------------------------------------------------ day arithmetic


@pytest.mark.parametrize(
    ("day", "expected"),
    [(1, "d1"), (2, "d2"), (7, "d2"), (0, "d1"), (-3, "d1")],
)
def test_the_beat_for_a_day_is_clamped_to_the_arc(day, expected) -> None:
    """A player who goes quiet for a fortnight gets the last beat, not a crash."""
    arc = parse_arc(MINIMAL)
    assert arc.beat_for_day(day).beat_id == expected


# ------------------------------------------------------------- validation


def test_an_arc_with_no_beats_is_rejected() -> None:
    with pytest.raises(ArcError, match="no beats"):
        parse_arc(variant(beats=[]))


def test_an_arc_with_no_characters_is_rejected() -> None:
    with pytest.raises(ArcError, match="no characters"):
        parse_arc(variant(characters=[]))


def test_duplicate_beat_ids_are_rejected() -> None:
    beats = [
        {"beat_id": "same", "day": 1, "title": "One", "goal": "g"},
        {"beat_id": "same", "day": 2, "title": "Two", "goal": "g"},
    ]
    with pytest.raises(ArcError, match="duplicate beat_id"):
        parse_arc(variant(beats=beats))


def test_beats_out_of_day_order_are_rejected() -> None:
    beats = [
        {"beat_id": "d2", "day": 2, "title": "Two", "goal": "g"},
        {"beat_id": "d1", "day": 1, "title": "One", "goal": "g"},
    ]
    with pytest.raises(ArcError, match="day order"):
        parse_arc(variant(beats=beats))


def test_a_day_missing_from_the_middle_is_rejected() -> None:
    """Otherwise day 2 silently plays day 3's beat for the rest of the week."""
    beats = [
        {"beat_id": "d1", "day": 1, "title": "One", "goal": "g"},
        {"beat_id": "d3", "day": 3, "title": "Three", "goal": "g"},
    ]
    with pytest.raises(ArcError, match="runs 2 days"):
        parse_arc(variant(beats=beats, duration_days=2))


def test_a_beat_missing_a_goal_is_rejected() -> None:
    beats = [
        {"beat_id": "d1", "day": 1, "title": "One"},
        {"beat_id": "d2", "day": 2, "title": "Two", "goal": "g"},
    ]
    with pytest.raises(ArcError, match="goal"):
        parse_arc(variant(beats=beats))


def test_an_unknown_arc_id_is_an_arc_error() -> None:
    with pytest.raises(ArcError, match="no arc named"):
        load_arc("does-not-exist")
