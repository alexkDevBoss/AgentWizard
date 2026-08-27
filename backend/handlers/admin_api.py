"""Admin API Lambda.

One function behind a Cognito JWT authorizer, routing on API Gateway's
``routeKey``. One function rather than eight: at ten players the whole console
is a trickle of traffic, and a single warm container answers everything.

Authentication is API Gateway's job -- the JWT authorizer rejects anything
unsigned before this code runs. What happens here is *authorisation context*:
every mutating call is attributed to the operator who made it and recorded on
the player's timeline, so the console never becomes an anonymous remote
control.

The manual-send route is the point of the whole phase. It goes through
``dispatch.send_to_player`` exactly like the story engine will, so an operator
message is subject to the same gates -- quiet hours, the daily rate limit,
content validation. When a gate refuses, the reason is returned to the UI
rather than swallowed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from backend.core import control, dispatch, logs, store
from backend.core.ids import enrolment_code
from backend.core.ids import player_id as new_player_id
from backend.core.models import (
    Channel,
    Direction,
    EventKind,
    MessageKind,
    Player,
    PlayerStatus,
)

DEFAULT_TIMELINE_LIMIT = 200
MAX_TIMELINE_LIMIT = 1000


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- plumbing


def _response(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            # CORS headers come from API Gateway, which is configured to
            # allow only the console's own origin. Caching a live player list
            # would be actively harmful, hence no-store.
            "cache-control": "no-store",
        },
        "body": json.dumps(body, default=str),
    }


def _operator(event: dict) -> str:
    """Who is making this call, from the verified JWT claims.

    API Gateway has already checked the signature; these claims cannot be
    forged by the caller.
    """
    claims = (
        ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}
    ).get("claims") or {}
    return (
        claims.get("email")
        or claims.get("cognito:username")
        or claims.get("sub")
        or "unknown"
    )


def _body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(400, "body is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise ApiError(400, "body must be a JSON object")
    return parsed


def _require_player(event: dict) -> Player:
    player_id = (event.get("pathParameters") or {}).get("player_id")
    if not player_id:
        raise ApiError(400, "player_id is required")
    player = store.get_player(player_id)
    if player is None:
        raise ApiError(404, f"no such player: {player_id}")
    return player


def _player_json(player: Player) -> dict:
    data = {field: getattr(player, field) for field in player.__dataclass_fields__}
    data["status"] = str(player.status)
    data["quota_used_today"] = store.quota_used(player.player_id, player.timezone)
    return data


# ----------------------------------------------------------------- routes


def get_me(event: dict) -> dict:
    return _response(200, {"operator": _operator(event)})


def list_players(_event: dict) -> dict:
    players = store.list_players()
    players.sort(key=lambda p: p.last_contact_at or p.created_at, reverse=True)
    return _response(200, {"players": [_player_json(p) for p in players]})


def get_player(event: dict) -> dict:
    player = _require_player(event)
    limit = (event.get("queryStringParameters") or {}).get("limit")
    try:
        count = min(int(limit), MAX_TIMELINE_LIMIT) if limit else DEFAULT_TIMELINE_LIMIT
    except ValueError:
        count = DEFAULT_TIMELINE_LIMIT

    return _response(
        200,
        {
            "player": _player_json(player),
            "timeline": store.timeline(player.player_id, limit=count),
        },
    )


def create_player(event: dict) -> dict:
    body = _body(event)
    name = (body.get("display_name") or "").strip()
    if not name:
        raise ApiError(400, "display_name is required")

    code = enrolment_code()
    player = Player(
        player_id=new_player_id(),
        display_name=name,
        timezone=(body.get("timezone") or "UTC").strip(),
        status=PlayerStatus.PENDING,
        enrolment_code=code,
        notes=(body.get("notes") or None),
    )
    store.put_player(player)
    store.put_enrolment_code(code, player.player_id)

    logs.info(
        "admin.player_created",
        player_id=player.player_id,
        operator=_operator(event),
    )
    return _response(201, {"player": _player_json(player), "enrolment_code": code})


def send_as_character(event: dict) -> dict:
    """The Wizard-of-Oz route: the operator writes, the character speaks.

    Deliberately *not* exempt from the gates. An operator typing at 23:30 is
    still sending a fictional character's message to a sleeping player, and the
    daily cap still bounds how much story a player receives. A refusal comes
    back with its reason so the console can show it.
    """
    player = _require_player(event)
    body = _body(event)
    text = (body.get("text") or "").strip()
    if not text:
        raise ApiError(400, "text is required")

    operator = _operator(event)
    result = dispatch.send_to_player(
        player,
        text,
        kind=MessageKind.STORY,
        beat_id=body.get("beat_id"),
        source=f"operator:{operator}",
    )

    logs.info(
        "admin.manual_send",
        player_id=player.player_id,
        operator=operator,
        sent=result.sent,
        reason=str(result.reason) if result.reason else None,
    )
    return _response(
        200 if result.sent else 409,
        {
            "sent": result.sent,
            "reason": str(result.reason) if result.reason else None,
            "retry_at": result.retry_at,
            "text": result.text,
        },
    )


def _control_route(action: str) -> Callable[[dict], dict]:
    def handler(event: dict) -> dict:
        player = _require_player(event)
        operator = _operator(event)
        kwargs: dict[str, Any] = {"source": f"admin:{operator}"}
        if action == "stop":
            kwargs["reason"] = (_body(event).get("reason") or "").strip() or (
                f"stopped by {operator}"
            )

        result = getattr(control, action)(player, **kwargs)
        refreshed = store.get_player(player.player_id)
        return _response(
            200,
            {
                "player": _player_json(refreshed) if refreshed else None,
                "acknowledged": result.sent,
                "reason": str(result.reason) if result.reason else None,
            },
        )

    return handler


def delete_player(event: dict) -> dict:
    """Delete a player and everything belonging to them.

    Backs the spec's "one command deletes a player and all their data". The
    player is stopped first, so nothing scheduled can fire against a record
    that is about to disappear.
    """
    player = _require_player(event)
    operator = _operator(event)

    if not player.is_stopped:
        control.cancel_scheduled_work(player.player_id, reason="player deleted")

    rows = store.delete_player(player.player_id)
    logs.warn(
        "admin.player_deleted",
        player_id=player.player_id,
        operator=operator,
        rows=rows,
    )
    # Phase 4 adds the S3 media sweep here.
    return _response(200, {"deleted": True, "rows": rows})


def add_note(event: dict) -> dict:
    """Pin an operator note onto the timeline.

    A running story needs somewhere to write "player said they're travelling
    Thursday" that survives the session.
    """
    player = _require_player(event)
    text = (_body(event).get("text") or "").strip()
    if not text:
        raise ApiError(400, "text is required")

    store.record_event(
        player.player_id,
        direction=Direction.OUT,
        channel=Channel.ADMIN,
        kind=EventKind.SYSTEM,
        text=text,
        note=True,
        source=f"operator:{_operator(event)}",
    )
    return _response(201, {"added": True})


ROUTES: dict[str, Callable[[dict], dict]] = {
    "GET /admin/me": get_me,
    "GET /admin/players": list_players,
    "POST /admin/players": create_player,
    "GET /admin/players/{player_id}": get_player,
    "DELETE /admin/players/{player_id}": delete_player,
    "POST /admin/players/{player_id}/message": send_as_character,
    "POST /admin/players/{player_id}/note": add_note,
    "POST /admin/players/{player_id}/stop": _control_route("stop"),
    "POST /admin/players/{player_id}/pause": _control_route("pause"),
    "POST /admin/players/{player_id}/resume": _control_route("resume"),
}


def handler(event: dict, _context: Any = None) -> dict:
    route_key = event.get("routeKey", "")
    route = ROUTES.get(route_key)
    if route is None:
        return _response(404, {"error": f"no route for {route_key}"})

    try:
        return route(event)
    except ApiError as exc:
        return _response(exc.status, {"error": exc.message})
    except Exception as exc:
        logs.error(
            "admin.request_failed",
            route=route_key,
            error=f"{type(exc).__name__}: {exc}",
        )
        return _response(500, {"error": "internal error"})
