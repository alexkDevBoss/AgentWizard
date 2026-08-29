"""Playing a walking arc.

The load-bearing property: **the story only moves when something verifiable
happened.** A coordinate inside a fence, or a photograph that arrived. Not a
model's opinion, and not the player simply saying they got there -- being told
"yes, they've arrived" while somebody is standing in the wrong street is the
failure that matters, because the next message sends them onward from a place
they never reached.

The other half is privacy: a position is compared against a fence and dropped.
Nothing that reaches the timeline, the model, or a log line can be turned back
into where somebody was standing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.core import engine, store
from backend.core.clock import iso
from backend.core.models import EventKind
from backend.story.arc import Advance, parse_arc

DAYTIME = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)

# Three real spots in central Tel Aviv, a couple of hundred metres apart.
FOUNDERS = (32.0650, 34.7750)
HALL = (32.0655, 34.7755)
GARDEN = (32.0670, 34.7770)
ELSEWHERE = (32.0900, 34.7900)  # ~2.7km north, well outside any fence


def waypoint(name: str, coords: tuple[float, float]) -> dict:
    return {
        "osm_id": f"node/{abs(hash(name)) % 9999}",
        "name": name,
        "kind": "memorial",
        "lat": coords[0],
        "lon": coords[1],
        "fence_m": 75,
    }


WALK = {
    "arc_id": "walk-test",
    "kind": "stages",
    "duration_days": 1,
    "title": "The Architect's List",
    "premise": "A conservator needs three photographs today.",
    "tone": "Warm and unhurried.",
    "characters": [{"name": "Dina", "role": "conservator", "voice": "short bursts"}],
    "beats": [
        {
            "beat_id": "s1-founders",
            "order": 1,
            "title": "The Founders' Stone",
            "goal": "Establish the theme.",
            "hint": "The memorial to the founders, near the boulevard.",
            "advance_on": "arrival",
            "waypoint": waypoint("Founders Monument", FOUNDERS),
        },
        {
            "beat_id": "s2-hall",
            "order": 2,
            "title": "Where the State Was Born",
            "goal": "Deepen it.",
            "hint": "Independence Hall, same stretch of boulevard.",
            "advance_on": "photo",
            "waypoint": waypoint("Independence Hall", HALL),
        },
        {
            "beat_id": "s3-garden",
            "order": 3,
            "title": "The Garden Between Pages",
            "goal": "Resolution.",
            "hint": "Ginat HaSharon, a small green patch.",
            "advance_on": "operator",
            "waypoint": waypoint("Ginat HaSharon", GARDEN),
        },
    ],
}


@pytest.fixture
def walker(player, table):
    """A player partway through a generated walking arc."""
    player.arc_id = "walk-test"
    player.arc_started_at = iso(DAYTIME)
    player.beat_order = 1
    store.put_player(player)
    store.put_arc(player.player_id, parse_arc(WALK).to_item())
    return player


def location_update() -> dict:
    return {"has_location": True, "has_photo": False, "text": None}


def photo_update() -> dict:
    return {"has_location": False, "has_photo": True, "text": None}


def text_update(text: str = "on my way") -> dict:
    return {"has_location": False, "has_photo": False, "text": text}


def order_of(player) -> int:
    return store.get_player(player.player_id).beat_order


# ------------------------------------------------------------- arc selection


def test_a_generated_arc_beats_the_shipped_default(walker, telegram, frozen) -> None:
    """Otherwise a customer's bespoke adventure quietly becomes Nightjar."""
    frozen(DAYTIME)
    ctx = engine.build_context(walker, now=DAYTIME)

    assert ctx.arc.arc_id == "walk-test"
    assert ctx.arc.is_walk
    assert ctx.beat.beat_id == "s1-founders"


def test_the_stage_pointer_decides_the_beat_not_the_calendar(
    walker, telegram, frozen
) -> None:
    """A walk is one day long; the date must not move anybody through it."""
    from datetime import timedelta

    store.set_beat_order(walker.player_id, 2)
    walker.beat_order = 2

    ctx = engine.build_context(walker, now=DAYTIME + timedelta(days=5))

    assert ctx.beat.beat_id == "s2-hall"


# --------------------------------------------------------------- arriving


def test_standing_at_the_waypoint_advances_the_stage(
    walker, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    engine.respond(walker, location_update(), coordinates=FOUNDERS)

    assert order_of(walker) == 2


def test_being_somewhere_else_does_not(walker, telegram, frozen, story) -> None:
    """The message that follows an arrival sends them onward. From nowhere."""
    frozen(DAYTIME)
    engine.respond(walker, location_update(), coordinates=ELSEWHERE)

    assert order_of(walker) == 1


def test_saying_you_arrived_does_not_advance_the_stage(
    walker, telegram, frozen, story
) -> None:
    """Claiming it is not being there, and only being there counts."""
    frozen(DAYTIME)
    engine.respond(walker, text_update("I'm at the monument now"))

    assert order_of(walker) == 1


def test_a_photo_does_not_satisfy_a_beat_waiting_for_an_arrival(
    walker, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    engine.respond(walker, photo_update())

    assert order_of(walker) == 1


def test_arriving_is_recorded_on_the_timeline(walker, telegram, frozen, story) -> None:
    frozen(DAYTIME)
    engine.respond(walker, location_update(), coordinates=FOUNDERS)

    located = [
        e for e in store.timeline(walker.player_id) if e["kind"] == EventKind.LOCATION
    ]
    assert located[-1]["arrived"] is True
    assert located[-1]["waypoint"] == "Founders Monument"


def test_a_completed_stage_records_how_it_was_completed(
    walker, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    engine.respond(walker, location_update(), coordinates=FOUNDERS)

    beat = store.get_beat(walker.player_id, "s1-founders")
    assert beat["completed_at"]
    assert beat["completed_by"] == str(Advance.ARRIVAL)


# ------------------------------------------------------------ photographing


def test_a_photo_advances_a_photo_stage(walker, telegram, frozen, story) -> None:
    frozen(DAYTIME)
    store.set_beat_order(walker.player_id, 2)
    walker.beat_order = 2

    engine.respond(walker, photo_update())

    assert order_of(walker) == 3


def test_standing_there_does_not_satisfy_a_photo_stage(
    walker, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    store.set_beat_order(walker.player_id, 2)
    walker.beat_order = 2

    engine.respond(walker, location_update(), coordinates=HALL)

    assert order_of(walker) == 2


# ------------------------------------------------------------- the last stage


def test_the_final_stage_never_completes_itself(
    walker, telegram, frozen, story
) -> None:
    """An arc that runs out leaves someone mid-street with nobody watching."""
    frozen(DAYTIME)
    store.set_beat_order(walker.player_id, 3)
    walker.beat_order = 3

    engine.respond(walker, location_update(), coordinates=GARDEN)
    engine.respond(walker, photo_update())
    engine.respond(walker, text_update("done!"))

    assert order_of(walker) == 3


# ------------------------------------------------------------------ privacy


def test_a_position_never_reaches_the_timeline(walker, telegram, frozen, story) -> None:
    frozen(DAYTIME)
    engine.respond(walker, location_update(), coordinates=ELSEWHERE)

    serialised = repr(store.timeline(walker.player_id))
    assert "32.09" not in serialised
    assert "34.79" not in serialised
    assert "latitude" not in serialised


def test_a_position_never_reaches_the_model(walker, telegram, frozen, story) -> None:
    frozen(DAYTIME)
    engine.respond(walker, location_update(), coordinates=ELSEWHERE)

    sent = repr(story.writes[-1])
    assert "32.09" not in sent
    assert "34.79" not in sent


def test_the_writer_is_given_a_band_not_a_distance(
    walker, telegram, frozen, story
) -> None:
    """A distance in metres from two waypoints triangulates a person."""
    frozen(DAYTIME)
    engine.respond(walker, location_update(), coordinates=ELSEWHERE)

    direction = story.last_messages[-1]["content"]
    assert "a fair walk" in direction or "across town" in direction
    assert "metres" not in direction.replace("in metres", "")


# ------------------------------------------------------------------ prompting


def test_the_walking_prompt_carries_the_route(walker, telegram, frozen, story) -> None:
    frozen(DAYTIME)
    engine.respond(walker, text_update())

    system = story.last_system
    assert "Founders Monument" in system
    assert "Independence Hall" in system
    assert "Ginat HaSharon" in system


def test_the_walking_prompt_replaces_the_stay_put_rule(
    walker, telegram, frozen, story
) -> None:
    """The armchair rule forbids the entire product; it must not survive here."""
    frozen(DAYTIME)
    engine.respond(walker, text_update())

    system = story.last_system
    assert "Never ask the player to go anywhere" not in system
    assert "walking adventure" in system


def test_the_walking_prompt_still_forbids_what_matters(
    walker, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    engine.respond(walker, text_update())

    prose = " ".join(story.last_system.split())
    assert "Never send them toward a person" in prose
    assert "Photographs are of things" in prose
    assert "No urgency" in prose
    assert "never write a coordinate" in prose
    # The universal rules are not dropped along the way.
    assert "Never claim to be a real person" in prose


def test_a_walking_message_is_still_gated_by_quiet_hours(
    walker, telegram, frozen, story
) -> None:
    """Being out on a walk is not a reason to be messaged at half past eleven."""
    frozen(datetime(2026, 3, 10, 23, 30, tzinfo=UTC))

    result = engine.respond(walker, location_update(), coordinates=FOUNDERS)

    assert result.blocked
    assert telegram.sent == []


def test_the_arrival_still_counted_even_though_the_reply_was_held(
    walker, telegram, frozen, story
) -> None:
    """Quiet hours silence the story; they do not rewind the player's progress."""
    frozen(datetime(2026, 3, 10, 23, 30, tzinfo=UTC))

    engine.respond(walker, location_update(), coordinates=FOUNDERS)

    assert order_of(walker) == 2
