"""The admin API.

The route that matters most is manual send: the whole Wizard-of-Oz pilot runs
through it, and it must be gated exactly like the story engine will be. If an
operator could bypass quiet hours or the daily cap by typing by hand, the
safety layer would be decorative.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from backend.core import store
from backend.core.models import PlayerStatus
from backend.handlers import admin_api

DAYTIME = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
NIGHT = datetime(2026, 3, 10, 23, 30, tzinfo=UTC)
OPERATOR = "alex@example.com"


def request(
    route: str,
    *,
    player_id: str | None = None,
    body: dict | None = None,
    query: dict | None = None,
) -> dict:
    return {
        "routeKey": route,
        "pathParameters": {"player_id": player_id} if player_id else None,
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


# ------------------------------------------------------------ manual send


def test_an_operator_message_reaches_the_player(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    status, body = call(
        "POST /admin/players/{player_id}/message",
        player_id=player.player_id,
        body={"text": "The envelope is under the bench."},
    )

    assert status == 200
    assert body["sent"] is True
    assert "The envelope is under the bench." in telegram.last


def test_an_operator_message_is_attributed_on_the_timeline(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    call(
        "POST /admin/players/{player_id}/message",
        player_id=player.player_id,
        body={"text": "Wait for the bell."},
    )

    outbound = store.timeline(player.player_id)[-1]
    assert outbound["source"] == f"operator:{OPERATOR}"


def test_quiet_hours_apply_to_the_operator_too(player, telegram, frozen) -> None:
    """Typing by hand at 23:30 is still a message to a sleeping player."""
    frozen(NIGHT)
    status, body = call(
        "POST /admin/players/{player_id}/message",
        player_id=player.player_id,
        body={"text": "Are you awake?"},
    )

    assert status == 409
    assert body["sent"] is False
    assert body["reason"] == "quiet_hours"
    assert body["retry_at"].startswith("2026-03-11T08:00")
    assert telegram.sent == []


def test_the_daily_cap_applies_to_the_operator_too(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    for i in range(6):
        call(
            "POST /admin/players/{player_id}/message",
            player_id=player.player_id,
            body={"text": f"beat {i}"},
        )

    status, body = call(
        "POST /admin/players/{player_id}/message",
        player_id=player.player_id,
        body={"text": "one too many"},
    )
    assert status == 409
    assert body["reason"] == "rate_limited"


def test_content_validation_applies_to_the_operator_too(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    status, body = call(
        "POST /admin/players/{player_id}/message",
        player_id=player.player_id,
        body={"text": "This is the police. Send your password."},
    )

    assert status == 200  # substituted, so the story still moves
    assert body["reason"] == "validation_failed"
    assert "password" not in telegram.last


def test_a_stopped_player_cannot_be_messaged_by_hand(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    call("POST /admin/players/{player_id}/stop", player_id=player.player_id)
    telegram.sent.clear()

    status, body = call(
        "POST /admin/players/{player_id}/message",
        player_id=player.player_id,
        body={"text": "one more thing"},
    )
    assert status == 409
    assert body["reason"] == "player_stopped"
    assert telegram.sent == []


def test_an_empty_message_is_refused(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    status, _ = call(
        "POST /admin/players/{player_id}/message",
        player_id=player.player_id,
        body={"text": "   "},
    )
    assert status == 400


# --------------------------------------------------------------- controls


@pytest.mark.parametrize(
    ("action", "expected"),
    [("stop", PlayerStatus.STOPPED), ("pause", PlayerStatus.PAUSED)],
)
def test_operator_controls_change_state(player, telegram, frozen, action, expected):
    frozen(DAYTIME)
    status, body = call(
        f"POST /admin/players/{{player_id}}/{action}", player_id=player.player_id
    )

    assert status == 200
    assert body["player"]["status"] == str(expected)
    assert store.get_player(player.player_id).status is expected


def test_an_operator_stop_records_who_did_it(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    call(
        "POST /admin/players/{player_id}/stop",
        player_id=player.player_id,
        body={"reason": "player asked me on the phone"},
    )

    stored = store.get_player(player.player_id)
    assert stored.stop_reason == "player asked me on the phone"
    commands = [e for e in store.timeline(player.player_id) if e["kind"] == "command"]
    assert commands[-1]["source"] == f"admin:{OPERATOR}"


def test_pause_then_resume_via_the_api(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    call("POST /admin/players/{player_id}/pause", player_id=player.player_id)
    status, body = call(
        "POST /admin/players/{player_id}/resume", player_id=player.player_id
    )
    assert body["player"]["status"] == "ACTIVE"


# --------------------------------------------------------------- players


def test_creating_a_player_returns_an_enrolment_code(table, telegram) -> None:
    status, body = call(
        "POST /admin/players",
        body={"display_name": "New Player", "timezone": "Europe/London"},
    )

    assert status == 201
    code = body["enrolment_code"]
    assert len(code) == 8
    assert body["player"]["status"] == "PENDING"
    assert store.player_id_for_code(code) == body["player"]["player_id"]


def test_creating_a_player_without_a_name_is_refused(table, telegram) -> None:
    status, _ = call("POST /admin/players", body={"timezone": "UTC"})
    assert status == 400


def test_the_player_list_is_ordered_by_most_recent_contact(
    player, telegram, frozen
) -> None:
    """The operator needs to see who has gone quiet, so freshest sits on top."""
    frozen(DAYTIME)
    _status, created = call("POST /admin/players", body={"display_name": "Stale One"})
    stale_id = created["player"]["player_id"]

    # Both timestamps are set explicitly rather than taken from the wall clock.
    # Creating one player and then touching the other raced: on Windows the
    # clock is coarse enough that both writes can land in the same microsecond,
    # leaving the sort order undefined and the test failing about one run in
    # five.
    store.touch_last_contact(stale_id, at="2026-03-10T10:00:00.000000Z")
    store.touch_last_contact(player.player_id, at="2026-03-10T11:00:00.000000Z")

    _status, body = call("GET /admin/players")
    order = [p["player_id"] for p in body["players"]]
    assert order == [player.player_id, stale_id]


def test_a_player_detail_carries_the_whole_timeline(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    call(
        "POST /admin/players/{player_id}/message",
        player_id=player.player_id,
        body={"text": "first"},
    )

    _status, body = call("GET /admin/players/{player_id}", player_id=player.player_id)
    assert body["player"]["quota_used_today"] == 1
    assert len(body["timeline"]) == 1


def test_deleting_a_player_removes_every_row(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    call(
        "POST /admin/players/{player_id}/message",
        player_id=player.player_id,
        body={"text": "something"},
    )

    status, body = call("DELETE /admin/players/{player_id}", player_id=player.player_id)
    assert status == 200
    assert body["rows"] > 0
    assert store.get_player(player.player_id) is None
    assert store.timeline(player.player_id) == []


def test_an_operator_note_lands_on_the_timeline(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    call(
        "POST /admin/players/{player_id}/note",
        player_id=player.player_id,
        body={"text": "says they're travelling Thursday"},
    )

    note = store.timeline(player.player_id)[-1]
    assert note["note"] is True
    assert note["channel"] == "admin"
    assert telegram.sent == []  # a note is not a message to the player


# -------------------------------------------------------------- plumbing


def test_the_operator_identity_comes_from_the_verified_jwt(table, telegram) -> None:
    _status, body = call("GET /admin/me")
    assert body["operator"] == OPERATOR


def test_an_unknown_player_is_a_404(table, telegram) -> None:
    status, _ = call("GET /admin/players/{player_id}", player_id="plr_nope")
    assert status == 404


def test_an_unknown_route_is_a_404(table, telegram) -> None:
    status, _ = call("GET /admin/nonsense")
    assert status == 404


def test_malformed_json_is_a_400(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    event = request(
        "POST /admin/players/{player_id}/message", player_id=player.player_id
    )
    event["body"] = "{not json"
    response = admin_api.handler(event)
    assert response["statusCode"] == 400


def test_responses_are_never_cached(table, telegram) -> None:
    """A stale player list during a running story would be actively misleading."""
    response = admin_api.handler(request("GET /admin/players"))
    assert response["headers"]["cache-control"] == "no-store"
