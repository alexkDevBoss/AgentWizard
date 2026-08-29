"""Composing a walk around a real place.

The property everything else rests on: **the model picks places from a list,
it never supplies a location.** A model that could name its own destination
would eventually name one that does not exist, or one across a motorway, and
the player would be the one to find that out on foot.

The rest is arithmetic the model cannot do and structure it is not trusted
with -- how far the walk actually is, and the rule that the last beat ends only
when a human says so.
"""

from __future__ import annotations

import pytest

from backend.core import arcsmith, places
from backend.core.geo import Point
from backend.core.places import Waypoint
from backend.story.arc import Advance, ArcKind

ORIGIN = Point(32.0640, 34.7740)


def waypoint(n: int, name: str, *, lat: float = 32.0650, lon: float = 34.7750):
    return Waypoint(
        osm_id=f"node/{n}",
        name=name,
        kind="artwork",
        lat=lat,
        lon=lon,
        distance_m=100.0 * n,
    )


#: Five places within a couple of hundred metres of each other.
CANDIDATES = [
    waypoint(1, "Founders Monument", lat=32.0650, lon=34.7750),
    waypoint(2, "Independence Hall", lat=32.0655, lon=34.7755),
    waypoint(3, "Rechter Plaque", lat=32.0660, lon=34.7760),
    waypoint(4, "Berlin Plaque", lat=32.0665, lon=34.7765),
    waypoint(5, "Ginat HaSharon", lat=32.0670, lon=34.7770),
]


def stage(number: int, task: str = "arrive") -> dict:
    return {
        "place_number": number,
        "title": f"Stage {number}",
        "goal": "Something happens here.",
        "hint": "Head for the thing.",
        "task": task,
    }


def draft(*stages: dict) -> dict:
    return {
        "title": "The Architect's List",
        "premise": "A conservator needs photographs.",
        "tone": "Warm and unhurried.",
        "character": {"name": "Dina", "role": "conservator", "voice": "short bursts"},
        "stages": list(stages),
    }


@pytest.fixture
def nearby(monkeypatch):
    """Return a fixed candidate list instead of calling Overpass."""
    state = {"result": list(CANDIDATES)}

    def fake(origin, **_kwargs):
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr(arcsmith, "find_nearby", fake)
    return state


# ------------------------------------------------------- the load-bearing rule


def test_a_place_that_was_not_offered_is_refused(nearby, story) -> None:
    """The one rule that keeps players out of places that may not exist."""
    story.verdict = draft(stage(1), stage(2), stage(99))

    with pytest.raises(arcsmith.ArcGenerationFailed, match="not among"):
        arcsmith.compose(ORIGIN, player_name="Alex")


@pytest.mark.parametrize("bogus", ["Founders Monument", None, -1, 0, 1.5])
def test_a_place_number_that_is_not_an_index_is_refused(nearby, story, bogus) -> None:
    story.verdict = draft(stage(1), stage(2), {**stage(3), "place_number": bogus})

    with pytest.raises(arcsmith.ArcGenerationFailed):
        arcsmith.compose(ORIGIN, player_name="Alex")


def test_the_same_place_twice_is_refused(nearby, story) -> None:
    story.verdict = draft(stage(1), stage(2), stage(1))

    with pytest.raises(arcsmith.ArcGenerationFailed, match="back to"):
        arcsmith.compose(ORIGIN, player_name="Alex")


def test_coordinates_come_from_the_lookup_not_the_model(nearby, story) -> None:
    story.verdict = draft(stage(1), stage(2), stage(3))

    arc = arcsmith.compose(ORIGIN, player_name="Alex").arc

    assert arc.beats[0].waypoint.lat == CANDIDATES[0].lat
    assert arc.beats[0].waypoint.osm_id == "node/1"


def test_the_model_is_never_shown_a_coordinate(nearby, story) -> None:
    story.verdict = draft(stage(1), stage(2), stage(3))
    arcsmith.compose(ORIGIN, player_name="Alex")

    prompt = story.reviews[-1]["messages"][0]["content"]
    assert "Founders Monument" in prompt
    for fragment in ("32.06", "34.77", "lat", "lon"):
        assert fragment not in prompt


# -------------------------------------------------------------- the structure


def test_the_last_beat_always_waits_for_the_operator(nearby, story) -> None:
    """An arc that self-completes leaves someone mid-street with nobody watching."""
    story.verdict = draft(stage(1), stage(2), stage(3, task="photograph"))

    arc = arcsmith.compose(ORIGIN, player_name="Alex").arc

    assert arc.beats[-1].advance_on is Advance.OPERATOR


def test_a_photograph_task_becomes_a_photo_gate(nearby, story) -> None:
    story.verdict = draft(stage(1, task="photograph"), stage(2), stage(3))

    arc = arcsmith.compose(ORIGIN, player_name="Alex").arc

    assert arc.beats[0].advance_on is Advance.PHOTO
    assert arc.beats[1].advance_on is Advance.ARRIVAL


def test_the_result_is_a_one_day_walk(nearby, story) -> None:
    story.verdict = draft(stage(1), stage(2), stage(3))

    arc = arcsmith.compose(ORIGIN, player_name="Alex").arc

    assert arc.kind is ArcKind.STAGES
    assert arc.duration_days == 1
    assert arc.is_walk


def test_the_arc_round_trips_through_storage(nearby, story) -> None:
    """A generated arc lives in DynamoDB, so it has to survive the trip."""
    from backend.story.arc import parse_arc

    story.verdict = draft(stage(1), stage(2, task="photograph"), stage(3))
    original = arcsmith.compose(ORIGIN, player_name="Alex").arc

    restored = parse_arc(original.to_item(), source="restored")

    assert restored.to_item() == original.to_item()
    assert restored.beats[1].advance_on is Advance.PHOTO
    assert restored.beats[0].waypoint.lat == original.beats[0].waypoint.lat


# ------------------------------------------------------- what a person can walk


def test_a_route_across_the_city_is_refused(nearby, story) -> None:
    """The model orders the stops; it cannot measure them."""
    nearby["result"] = [
        waypoint(1, "Here", lat=32.0650, lon=34.7750),
        waypoint(2, "Also here", lat=32.0655, lon=34.7755),
        waypoint(3, "Haifa", lat=32.7940, lon=34.9896),
    ]
    story.verdict = draft(stage(1), stage(2), stage(3))

    with pytest.raises(arcsmith.ArcGenerationFailed, match="km"):
        arcsmith.compose(ORIGIN, player_name="Alex")


def test_one_impossible_leg_is_refused_even_in_a_short_route(nearby, story) -> None:
    nearby["result"] = [
        waypoint(1, "Start", lat=32.0650, lon=34.7750),
        waypoint(2, "Two km away", lat=32.0830, lon=34.7750),
        waypoint(3, "Back again", lat=32.0655, lon=34.7755),
    ]
    story.verdict = draft(stage(1), stage(2), stage(3))

    with pytest.raises(arcsmith.ArcGenerationFailed, match="leg"):
        arcsmith.compose(ORIGIN, player_name="Alex")


@pytest.mark.parametrize("count", [1, 2, 7])
def test_too_few_or_too_many_stages_are_refused(nearby, story, count) -> None:
    story.verdict = draft(*[stage(min(i, 5)) for i in range(1, count + 1)])

    with pytest.raises(arcsmith.ArcGenerationFailed):
        arcsmith.compose(ORIGIN, player_name="Alex")


# ------------------------------------------------------------ thin and broken


def test_an_area_with_nothing_mapped_says_so_plainly(nearby, story) -> None:
    """Overpass is thin in suburbs. The operator needs a reason, not a stack trace."""
    nearby["result"] = [waypoint(1, "The only bench")]

    with pytest.raises(arcsmith.ArcGenerationFailed, match="not enough"):
        arcsmith.compose(ORIGIN, player_name="Alex")


def test_overpass_being_down_is_reported_as_a_generation_failure(nearby, story) -> None:
    nearby["result"] = places.PlacesUnavailable("no endpoint answered")

    with pytest.raises(arcsmith.ArcGenerationFailed, match="nearby places"):
        arcsmith.compose(ORIGIN, player_name="Alex")


def test_the_writer_being_unreachable_is_reported_too(nearby, story) -> None:
    from backend.core import model

    story.review_error = model.ModelUnavailable("timeout")

    with pytest.raises(arcsmith.ArcGenerationFailed, match="could not be reached"):
        arcsmith.compose(ORIGIN, player_name="Alex")


# ------------------------------------------------------------------- prompting


def test_the_writer_is_told_the_hard_rules_about_real_people() -> None:
    prose = " ".join(arcsmith.SYSTEM.split())

    assert "Never send them toward a person" in prose
    assert "Photographs are of things, never of people" in prose
    assert "No urgency, no timers" in prose
    assert "Daylight, public places" in prose


def test_a_requested_theme_reaches_the_writer(nearby, story) -> None:
    story.verdict = draft(stage(1), stage(2), stage(3))

    arcsmith.compose(ORIGIN, player_name="Alex", theme="something about birds")

    assert "something about birds" in story.reviews[-1]["messages"][0]["content"]
