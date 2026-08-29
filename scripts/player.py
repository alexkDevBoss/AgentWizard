#!/usr/bin/env python
"""Operator CLI for players.

    python scripts/player.py add --name "Alex" --tz Europe/London
    python scripts/player.py compose plr_xxx --at 32.0640,34.7740
    python scripts/player.py list
    python scripts/player.py show plr_xxx
    python scripts/player.py stop plr_xxx --reason "operator call"
    python scripts/player.py pause plr_xxx
    python scripts/player.py resume plr_xxx
    python scripts/player.py delete plr_xxx --yes

Every action goes through the same ``backend.core`` code the Lambda uses, so
an operator STOP is the same STOP the player gets.
"""

from __future__ import annotations

import argparse
import sys

from _common import add_env_argument, bootstrap, confirm


def cmd_add(args) -> int:
    from backend.core import store
    from backend.core.ids import enrolment_code, player_id
    from backend.core.models import Player, PlayerStatus

    code = enrolment_code()
    player = Player(
        player_id=player_id(),
        display_name=args.name,
        timezone=args.tz,
        status=PlayerStatus.PENDING,
        enrolment_code=code,
        notes=args.notes,
    )
    store.put_player(player)
    store.put_enrolment_code(code, player.player_id)

    print(f"created {player.player_id}  ({player.display_name}, {player.timezone})")
    print()
    print("Ask them to send this to the bot:")
    print(f"    /start {code}")
    print()
    print("They are PENDING until they do. The code is single-use.")
    return 0


def cmd_list(args) -> int:
    from backend.core import store

    players = sorted(store.list_players(), key=lambda p: p.created_at)
    if not players:
        print("no players yet -- add one with `player.py add`")
        return 0

    print(f"{'player_id':<20} {'status':<9} {'tz':<18} {'last contact':<28} name")
    print("-" * 100)
    for p in players:
        print(
            f"{p.player_id:<20} {str(p.status):<9} {p.timezone:<18} "
            f"{(p.last_contact_at or '-'):<28} {p.display_name}"
        )
    return 0


def cmd_show(args) -> int:
    from backend.core import store

    player = store.get_player(args.player_id)
    if player is None:
        print(f"no such player: {args.player_id}", file=sys.stderr)
        return 1

    print(f"{player.display_name}  ({player.player_id})")
    print(f"  status        {player.status}")
    print(f"  timezone      {player.timezone}")
    print(f"  telegram      {player.telegram_chat_id or '(not bound)'}")
    print(f"  arc           {player.arc_id or '-'}")
    print(f"  last contact  {player.last_contact_at or '-'}")
    if player.is_stopped:
        print(f"  stopped       {player.stopped_at} ({player.stop_reason})")
    print(f"  quota today   {store.quota_used(player.player_id, player.timezone)}")
    print()

    events = store.timeline(player.player_id, limit=args.limit)
    if not events:
        print("  (no events)")
        return 0

    print("  timeline")
    for event in events:
        arrow = "->" if event["direction"] == "out" else "<-"
        flag = " !" if event.get("needs_review") else "  "
        text = (event.get("text") or "").replace("\n", " ")
        if len(text) > 88:
            text = text[:85] + "..."
        reason = f" [{event['reason']}]" if event.get("reason") else ""
        print(f"  {event['at']} {arrow}{flag}{event['kind']:<9}{reason} {text}")
    return 0


def _control(args, action: str) -> int:
    from backend.core import control, store

    player = store.get_player(args.player_id)
    if player is None:
        print(f"no such player: {args.player_id}", file=sys.stderr)
        return 1

    fn = getattr(control, action)
    kwargs = {"source": "admin"}
    if action == "stop":
        kwargs["reason"] = args.reason
    result = fn(player, **kwargs)

    print(f"{action}: {player.player_id} -> {store.get_player(args.player_id).status}")
    if result.blocked:
        print(f"  (acknowledgement not delivered: {result.reason})")
    return 0


def cmd_compose(args) -> int:
    """Build a one-day walking arc around a real location and store it.

    Until the onboarding site exists, this is how a customer's start location
    becomes an adventure. The location is used to search and then dropped --
    what is stored against the player is a list of public places, never where
    they were standing.
    """
    from backend.core import arcsmith, store
    from backend.core.geo import Point

    player = store.get_player(args.player_id)
    if player is None:
        print(f"no such player: {args.player_id}", file=sys.stderr)
        return 1

    try:
        lat, lon = (float(part) for part in args.at.split(","))
        origin = Point(lat, lon)
    except ValueError as exc:
        print(f"--at wants 'lat,lon' -- {exc}", file=sys.stderr)
        return 1

    print(f"composing a walk near {args.at} for {player.display_name}...")
    try:
        draft = arcsmith.compose(
            origin, player_name=player.display_name, theme=args.theme or ""
        )
    except arcsmith.ArcGenerationFailed as exc:
        print(f"could not compose an arc: {exc}", file=sys.stderr)
        return 1

    arc = draft.arc
    print()
    print(f"  {arc.title}   [{arc.arc_id}]")
    print(f"  {arc.length} stops, {draft.route_m:.0f}m of walking")
    print()
    for beat in arc.beats:
        waits = {"arrival": "arrive", "photo": "photograph", "operator": "you end it"}
        print(
            f"  {beat.order}. {beat.waypoint.name}"
            f"  [{waits.get(str(beat.advance_on), beat.advance_on)}]"
        )
    print()

    if not args.yes and not confirm("Store this arc for the player?"):
        print("not stored. Run again for a different one.")
        return 1

    store.put_arc(player.player_id, arc.to_item())
    store.set_beat_order(player.player_id, 1)
    player.arc_id = arc.arc_id
    store.put_player(player)
    print(f"stored. {player.display_name} is on step 1 of {arc.length}.")
    return 0


def cmd_delete(args) -> int:
    from backend.core import store

    player = store.get_player(args.player_id)
    if player is None:
        print(f"no such player: {args.player_id}", file=sys.stderr)
        return 1

    print(f"About to permanently delete {player.display_name} ({player.player_id})")
    print("This removes their profile, their whole timeline, and every binding.")
    if not confirm("Delete?", assume_yes=args.yes):
        print("aborted")
        return 1

    count = store.delete_player(args.player_id)
    print(f"deleted {count} rows")
    # Phase 4 adds the S3 media sweep here.
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_env_argument(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="create a player and mint an enrolment code")
    add.add_argument("--name", required=True)
    add.add_argument("--tz", default="UTC", help="IANA timezone, e.g. Europe/London")
    add.add_argument("--notes", default=None)
    add.set_defaults(func=cmd_add)

    compose = sub.add_parser(
        "compose", help="build a one-day walking arc around a real location"
    )
    compose.add_argument("player_id")
    compose.add_argument(
        "--at", required=True, help="starting point as 'lat,lon' (decimal degrees)"
    )
    compose.add_argument("--theme", help="what the customer asked for, if anything")
    compose.add_argument("--yes", action="store_true", help="store without confirming")
    compose.set_defaults(func=cmd_compose)

    listing = sub.add_parser("list", help="list every player")
    listing.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="profile and timeline for one player")
    show.add_argument("player_id")
    show.add_argument("--limit", type=int, default=50)
    show.set_defaults(func=cmd_show)

    stop = sub.add_parser("stop", help="halt a player's story immediately")
    stop.add_argument("player_id")
    stop.add_argument("--reason", default="operator stopped")
    stop.set_defaults(func=lambda a: _control(a, "stop"))

    pause = sub.add_parser("pause", help="suspend a player's story")
    pause.add_argument("player_id")
    pause.set_defaults(func=lambda a: _control(a, "pause"))

    resume = sub.add_parser("resume", help="continue a paused story")
    resume.add_argument("player_id")
    resume.set_defaults(func=lambda a: _control(a, "resume"))

    delete = sub.add_parser("delete", help="delete a player and all their data")
    delete.add_argument("player_id")
    delete.add_argument("--yes", action="store_true", help="skip the confirmation")
    delete.set_defaults(func=cmd_delete)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    env = bootstrap(args.env)
    print(f"[{env}]")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
