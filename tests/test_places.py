"""The OpenStreetMap lookup.

Overpass is a donated public service, so no test here touches it. The
behaviour that matters is what happens to its answers: unusable elements are
dropped, one category cannot fill the whole list, and an endpoint that dies
mid-request moves on to the mirror instead of taking onboarding down with it.
"""

from __future__ import annotations

import http.client
import urllib.error

import pytest

from backend.core import places
from backend.core.geo import Point

ORIGIN = Point(32.0640, 34.7740)


def element(
    osm_id: int, name: str, key: str = "tourism", value: str = "artwork", **extra
) -> dict:
    return {
        "type": "node",
        "id": osm_id,
        "lat": 32.0650,
        "lon": 34.7750,
        "tags": {"name": name, key: value},
        **extra,
    }


def payload(*elements: dict) -> dict:
    return {"elements": list(elements)}


@pytest.fixture
def overpass(monkeypatch):
    """Answer every Overpass call from a canned payload."""
    state = {"payload": payload(), "calls": []}

    def fake_post(url, query):
        state["calls"].append((url, query))
        result = state["payload"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(places, "_post", fake_post)
    return state


# ------------------------------------------------------------------ parsing


def test_a_named_node_becomes_a_waypoint(overpass) -> None:
    overpass["payload"] = payload(element(1, "Meir Dizengoff"))

    found = places.find_nearby(ORIGIN)

    assert len(found) == 1
    assert found[0].name == "Meir Dizengoff"
    assert found[0].kind == "artwork"
    assert found[0].osm_id == "node/1"


def test_an_unnamed_element_is_dropped(overpass) -> None:
    """A waypoint the player cannot be told the name of is not a waypoint."""
    nameless = {"type": "node", "id": 2, "lat": 32.06, "lon": 34.77, "tags": {}}
    overpass["payload"] = payload(nameless, element(1, "Meir Dizengoff"))

    assert [w.name for w in places.find_nearby(ORIGIN)] == ["Meir Dizengoff"]


def test_a_way_is_read_from_its_centre(overpass) -> None:
    """Parks come back as outlines; nobody can stand "at" an outline."""
    park = {
        "type": "way",
        "id": 7,
        "center": {"lat": 32.07, "lon": 34.78},
        "tags": {"name": "Gan Meir", "leisure": "park"},
    }
    overpass["payload"] = payload(park)

    found = places.find_nearby(ORIGIN)
    assert found[0].lat == 32.07
    assert found[0].osm_id == "way/7"


def test_an_element_with_no_usable_position_is_dropped(overpass) -> None:
    broken = {"type": "way", "id": 8, "tags": {"name": "Nowhere", "leisure": "park"}}
    overpass["payload"] = payload(broken)

    assert places.find_nearby(ORIGIN) == []


def test_duplicate_names_are_collapsed(overpass) -> None:
    """OSM maps the same monument as several elements more often than you'd think."""
    overpass["payload"] = payload(
        element(1, "Founders Monument"), element(2, "founders monument")
    )

    assert len(places.find_nearby(ORIGIN)) == 1


def test_a_non_latin_name_survives(overpass) -> None:
    overpass["payload"] = payload(element(1, "שוק לוינסקי", "amenity", "marketplace"))

    assert places.find_nearby(ORIGIN)[0].name == "שוק לוינסקי"


def test_an_empty_area_is_an_empty_list_not_an_error(overpass) -> None:
    """A suburb with nothing mapped is a real answer about a real place."""
    assert places.find_nearby(ORIGIN) == []


# -------------------------------------------------------------------- ranking


def test_a_better_destination_outranks_a_nearer_one(overpass) -> None:
    overpass["payload"] = payload(
        element(1, "Swings", "leisure", "playground"),
        element(2, "The Fountain", "amenity", "fountain"),
    )

    assert places.find_nearby(ORIGIN)[0].name == "The Fountain"


def test_one_category_cannot_fill_the_whole_list(overpass) -> None:
    """A route of five statues is a worse walk than a statue, a park and a museum."""
    statues = [element(i, f"Statue {i}") for i in range(10)]
    park = element(99, "Gan Meir", "leisure", "park")
    overpass["payload"] = payload(*statues, park)

    found = places.find_nearby(ORIGIN, limit=4)

    assert sum(w.kind == "artwork" for w in found) == places.MAX_PER_KIND
    assert "Gan Meir" in [w.name for w in found]


def test_a_thin_area_still_backfills_to_a_usable_route(overpass) -> None:
    """Where statues are all there is, return statues rather than a short list."""
    overpass["payload"] = payload(*[element(i, f"Statue {i}") for i in range(10)])

    assert len(places.find_nearby(ORIGIN, limit=6)) == 6


# ------------------------------------------------------------------ endpoints


def test_a_dead_endpoint_falls_through_to_the_mirror(monkeypatch) -> None:
    calls: list[str] = []

    def flaky(url, _query):
        calls.append(url)
        if len(calls) == 1:
            raise urllib.error.URLError("down")
        return payload(element(1, "Meir Dizengoff"))

    monkeypatch.setattr(places, "_post", flaky)

    assert places.find_nearby(ORIGIN)[0].name == "Meir Dizengoff"
    assert calls == list(places.ENDPOINTS)


def test_load_shedding_falls_through_too(monkeypatch) -> None:
    """Overpass sheds load by hanging up, which is not a URLError.

    This is not hypothetical -- it is how the first live run of this module
    failed, and the obvious except clause did not catch it.
    """
    calls: list[str] = []

    def hangs_up(url, _query):
        calls.append(url)
        if len(calls) == 1:
            raise http.client.RemoteDisconnected("closed without response")
        return payload(element(1, "Meir Dizengoff"))

    monkeypatch.setattr(places, "_post", hangs_up)

    assert places.find_nearby(ORIGIN)[0].name == "Meir Dizengoff"


def test_every_endpoint_failing_raises_rather_than_inventing_places(overpass) -> None:
    overpass["payload"] = TimeoutError("read timed out")

    with pytest.raises(places.PlacesUnavailable):
        places.find_nearby(ORIGIN)


# -------------------------------------------------------------------- privacy


def test_a_waypoint_rendered_for_a_prompt_carries_no_coordinates(overpass) -> None:
    """Shown coordinates, a model will eventually write them out to the player."""
    overpass["payload"] = payload(element(1, "Meir Dizengoff"))

    rendered = places.find_nearby(ORIGIN)[0].render()

    assert rendered == "Meir Dizengoff (artwork)"
    assert "32.0" not in rendered and "34.7" not in rendered
