"""Composing and editing an arc from the console.

Two things are being pinned. First, that composing is a *background* job:
it takes 25-35 seconds against Bedrock and API Gateway hangs up at 30, so the
API must never try to do the work itself.

Second -- and this is the one that protects a real person -- that an edited arc
is checked exactly as hard as a generated one. The operator can rewrite
anything, but swapping one stop for another is precisely how an editable arc
grows a five-kilometre leg, and the player is the one who would find that out
on foot.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from backend.core import arcsmith, places, store
from backend.handlers import admin_api, arc_composer
from backend.story.arc import parse_arc

DAYTIME = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
OPERATOR = "alex@example.com"

FOUNDERS = (32.0664, 34.7712)
HALL = (32.0640, 34.7740)
GARDEN = (32.0680, 34.7700)
HAIFA = (32.7940, 34.9896)


def waypoint(name: str, coords: tuple[float, float]) -> dict:
    return {
        "osm_id": f"node/{abs(hash(name)) % 9999}",
        "name": name,
        "kind": "memorial",
        "lat": coords[0],
        "lon": coords[1],
        "fence_m": 75,
    }


def arc_item(third: tuple[float, float] = GARDEN) -> dict:
    return {
        "arc_id": "walk-test",
        "kind": "stages",
        "duration_days": 1,
        "title": "The Architect's List",
        "premise": "A conservator needs photographs.",
        "tone": "Warm and unhurried.",
        "characters": [{"name": "Dina", "role": "conservator", "voice": "bursts"}],
        "beats": [
            {
                "beat_id": "s1",
                "order": 1,
                "title": "One",
                "goal": "g",
                "hint": "h",
                "advance_on": "arrival",
                "waypoint": waypoint("Founders Monument", FOUNDERS),
            },
            {
                "beat_id": "s2",
                "order": 2,
                "title": "Two",
                "goal": "g",
                "hint": "h",
                "advance_on": "photo",
                "waypoint": waypoint("Independence Hall", HALL),
            },
            {
                "beat_id": "s3",
                "order": 3,
                "title": "Three",
                "goal": "g",
                "hint": "h",
                "advance_on": "operator",
                "waypoint": waypoint("Ginat HaSharon", third),
            },
        ],
    }


def request(route: str, *, path: dict | None = None, body=None, query=None) -> dict:
    return {
        "routeKey": route,
        "pathParameters": path,
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "http": {"method": route.split(" ")[0]},
            "authorizer": {"jwt": {"claims": {"email": OPERATOR}}},
        },
    }


def call(route: str, **kwargs) -> tuple[int, dict]:
    response = admin_api.handler(request(route, **kwargs))
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture
def invoker(monkeypatch):
    """Capture the async invoke instead of calling Lambda."""

    class FakeLambda:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def invoke(self, **kwargs):
            kwargs["Payload"] = json.loads(kwargs["Payload"])
            self.calls.append(kwargs)
            return {"StatusCode": 202}

    fake = FakeLambda()
    monkeypatch.setattr(admin_api, "_lambda", lambda: fake)
    monkeypatch.setenv(admin_api.COMPOSER_ENV, "adventure-agent-test-arc-composer")
    return fake


# ------------------------------------------------------------ starting a job


def test_composing_starts_a_job_rather_than_doing_the_work(
    player, telegram, invoker
) -> None:
    """35 seconds of writing cannot happen inside a 30-second HTTP request."""
    status, body = call(
        "POST /admin/players/{player_id}/arc/compose",
        path={"player_id": player.player_id},
        body={"at": "32.0640,34.7740"},
    )

    assert status == 202
    assert body["status"] == "running"
    assert body["job_id"]


def test_the_job_is_handed_to_the_composer_function(player, telegram, invoker) -> None:
    call(
        "POST /admin/players/{player_id}/arc/compose",
        path={"player_id": player.player_id},
        body={"at": "32.0640,34.7740", "theme": "something about birds"},
    )

    sent = invoker.calls[-1]
    assert sent["InvocationType"] == "Event", "must not block on the composer"
    assert sent["Payload"]["lat"] == 32.0640
    assert sent["Payload"]["theme"] == "something about birds"


def test_the_job_row_exists_before_the_response_returns(
    player, telegram, invoker
) -> None:
    """A console that polls immediately must not get a 404."""
    _status, body = call(
        "POST /admin/players/{player_id}/arc/compose",
        path={"player_id": player.player_id},
        body={"at": "32.0640,34.7740"},
    )

    job = store.get_arc_job(player.player_id, body["job_id"])
    assert job["status"] == "running"


@pytest.mark.parametrize(
    "at", ["", "not-a-point", "32.0640", "91,0", "0,181", "abc,def"]
)
def test_a_starting_point_that_is_not_a_point_is_refused(
    player, telegram, invoker, at
) -> None:
    status, _body = call(
        "POST /admin/players/{player_id}/arc/compose",
        path={"player_id": player.player_id},
        body={"at": at},
    )
    assert status == 400


def test_polling_an_unknown_job_is_a_404(player, telegram) -> None:
    status, _body = call(
        "GET /admin/players/{player_id}/arc/jobs/{job_id}",
        path={"player_id": player.player_id, "job_id": "nope"},
    )
    assert status == 404


def test_polling_returns_the_draft_once_it_is_done(player, telegram) -> None:
    store.put_arc_job(
        player.player_id, "j1", status="done", arc=arc_item(), route_m=1348
    )

    status, body = call(
        "GET /admin/players/{player_id}/arc/jobs/{job_id}",
        path={"player_id": player.player_id, "job_id": "j1"},
    )

    assert status == 200
    assert body["job"]["status"] == "done"
    assert body["job"]["arc"]["title"] == "The Architect's List"


# ------------------------------------------------------------- the composer


def test_the_composer_stores_the_finished_draft(player, telegram, monkeypatch) -> None:
    arc = parse_arc(arc_item())
    stops = [b.waypoint for b in arc.beats]
    monkeypatch.setattr(
        arcsmith,
        "compose",
        lambda *a, **k: arcsmith.Draft(arc=arc, route_m=1348.0, candidates=stops),
    )

    arc_composer.handler(
        {
            "player_id": player.player_id,
            "job_id": "j2",
            "lat": 32.064,
            "lon": 34.774,
        }
    )

    job = store.get_arc_job(player.player_id, "j2")
    assert job["status"] == arc_composer.DONE
    assert job["arc"]["title"] == "The Architect's List"
    assert job["route_m"] == 1348
    # The candidate list rides along so the editor can offer swaps without a
    # second trip to a donated public service.
    assert len(job["candidates"]) == 3


def test_a_failed_compose_says_why(player, telegram, monkeypatch) -> None:
    """ "It failed" is a dead end; the real reason is usually specific."""

    def boom(*_a, **_k):
        raise arcsmith.ArcGenerationFailed("only 1 mapped place within 1400m")

    monkeypatch.setattr(arcsmith, "compose", boom)

    arc_composer.handler(
        {"player_id": player.player_id, "job_id": "j3", "lat": 0.0, "lon": 0.0}
    )

    job = store.get_arc_job(player.player_id, "j3")
    assert job["status"] == arc_composer.FAILED
    assert "1 mapped place" in job["error"]


def test_an_unexpected_crash_never_leaves_a_job_running(
    player, telegram, monkeypatch
) -> None:
    """A job stuck on 'running' is a spinner that never stops."""

    def boom(*_a, **_k):
        raise RuntimeError("something else entirely")

    monkeypatch.setattr(arcsmith, "compose", boom)

    arc_composer.handler(
        {"player_id": player.player_id, "job_id": "j4", "lat": 0.0, "lon": 0.0}
    )

    assert store.get_arc_job(player.player_id, "j4")["status"] == arc_composer.FAILED


# ---------------------------------------------------------------- editing


def test_an_edited_arc_is_saved(player, telegram) -> None:
    status, body = call(
        "PUT /admin/players/{player_id}/arc",
        path={"player_id": player.player_id},
        body={"arc": arc_item()},
    )

    assert status == 200
    assert store.get_arc(player.player_id)["title"] == "The Architect's List"
    assert body["arc"]["beats"][0]["beat_id"] == "s1"


def test_an_edited_arc_is_checked_as_hard_as_a_generated_one(player, telegram) -> None:
    """Swapping a stop is how an editable arc grows a leg nobody can walk."""
    status, body = call(
        "PUT /admin/players/{player_id}/arc",
        path={"player_id": player.player_id},
        body={"arc": arc_item(third=HAIFA)},
    )

    assert status == 422
    assert "km" in body["error"] or "leg" in body["error"]
    assert store.get_arc(player.player_id) is None, "nothing unsafe was saved"


def test_an_arc_that_does_not_validate_is_refused(player, telegram) -> None:
    broken = arc_item()
    broken["beats"][-1]["advance_on"] = "arrival"  # the last beat must wait for a human

    status, body = call(
        "PUT /admin/players/{player_id}/arc",
        path={"player_id": player.player_id},
        body={"arc": broken},
    )

    assert status == 422
    assert "operator" in body["error"]


def test_saving_an_arc_shows_up_on_the_timeline(player, telegram) -> None:
    """The console is never an anonymous remote control."""
    call(
        "PUT /admin/players/{player_id}/arc",
        path={"player_id": player.player_id},
        body={"arc": arc_item()},
    )

    last = store.timeline(player.player_id)[-1]
    assert "Arc saved" in last["text"]
    assert last["source"] == f"operator:{OPERATOR}"


def test_saving_can_send_the_player_back_to_the_first_stop(player, telegram) -> None:
    store.set_beat_order(player.player_id, 3)

    call(
        "PUT /admin/players/{player_id}/arc",
        path={"player_id": player.player_id},
        body={"arc": arc_item(), "restart": True},
    )

    assert store.get_player(player.player_id).beat_order == 1


def test_saving_without_restart_leaves_progress_alone(player, telegram) -> None:
    store.set_beat_order(player.player_id, 2)

    call(
        "PUT /admin/players/{player_id}/arc",
        path={"player_id": player.player_id},
        body={"arc": arc_item()},
    )

    assert store.get_player(player.player_id).beat_order == 2


def test_reading_back_an_arc_that_was_never_composed_is_a_404(player, telegram) -> None:
    status, _body = call(
        "GET /admin/players/{player_id}/arc", path={"player_id": player.player_id}
    )
    assert status == 404


# ------------------------------------------------------- places for swapping


def test_the_editor_can_ask_what_else_is_nearby(player, telegram, monkeypatch) -> None:
    monkeypatch.setattr(
        places,
        "find_nearby",
        lambda *a, **k: [
            places.Waypoint(
                osm_id="node/1", name="Gan Meir", kind="park", lat=32.07, lon=34.77
            )
        ],
    )

    status, body = call(
        "GET /admin/players/{player_id}/places",
        path={"player_id": player.player_id},
        query={"at": "32.0640,34.7740"},
    )

    assert status == 200
    assert body["places"][0]["name"] == "Gan Meir"


def test_overpass_being_down_is_a_503_not_a_500(player, telegram, monkeypatch) -> None:
    def down(*_a, **_k):
        raise places.PlacesUnavailable("no endpoint answered")

    monkeypatch.setattr(places, "find_nearby", down)

    status, _body = call(
        "GET /admin/players/{player_id}/places",
        path={"player_id": player.player_id},
        query={"at": "32.0640,34.7740"},
    )

    assert status == 503
