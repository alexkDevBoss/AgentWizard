"""Compose an arc in the background.

Invoked asynchronously by the admin API, never by API Gateway. It exists
because composing takes 25-35 seconds against Bedrock and API Gateway closes
an HTTP connection at 30 -- so the console starts a job, this finishes it, and
the two halves talk through a row in DynamoDB.

Nothing here is player-facing. Every failure is written to the job for the
operator to read, because "compose failed" with no reason is a dead end when
the real answer is usually specific: Overpass was down, or the area has
nothing mapped, or the route came out too long to walk.
"""

from __future__ import annotations

from typing import Any

from backend.core import arcsmith, logs, store
from backend.core.geo import Point

#: Job states the console polls for.
RUNNING = "running"
DONE = "done"
FAILED = "failed"


def handler(event: dict, _context: Any = None) -> dict:
    player_id = event.get("player_id")
    job_id = event.get("job_id")
    if not player_id or not job_id:
        logs.error("arc_composer.bad_event", event=event)
        return {"ok": False}

    try:
        origin = Point(float(event["lat"]), float(event["lon"]))
    except (KeyError, TypeError, ValueError) as exc:
        _fail(player_id, job_id, f"bad starting point: {exc}")
        return {"ok": False}

    player = store.get_player(player_id)
    if player is None:
        _fail(player_id, job_id, f"no such player: {player_id}")
        return {"ok": False}

    try:
        with logs.timed("arc_composer.compose", player_id=player_id, job_id=job_id):
            draft = arcsmith.compose(
                origin,
                player_name=player.display_name,
                theme=(event.get("theme") or ""),
            )
    except arcsmith.ArcGenerationFailed as exc:
        _fail(player_id, job_id, str(exc))
        return {"ok": False}
    except Exception as exc:  # never leave a job stuck on "running"
        logs.error(
            "arc_composer.unexpected",
            player_id=player_id,
            job_id=job_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        _fail(player_id, job_id, f"unexpected failure: {type(exc).__name__}")
        return {"ok": False}

    # The candidate list is stored with the draft so the console's editor can
    # offer alternatives for each stop without a second trip to Overpass.
    store.put_arc_job(
        player_id,
        job_id,
        status=DONE,
        arc=draft.arc.to_item(),
        route_m=round(draft.route_m),
        candidates=[w.to_item() for w in draft.candidates],
    )
    logs.info(
        "arc_composer.done",
        player_id=player_id,
        job_id=job_id,
        stages=draft.arc.length,
        route_m=round(draft.route_m),
    )
    return {"ok": True}


def _fail(player_id: str, job_id: str, reason: str) -> None:
    logs.warn("arc_composer.failed", player_id=player_id, job_id=job_id, reason=reason)
    store.put_arc_job(player_id, job_id, status=FAILED, error=reason)
