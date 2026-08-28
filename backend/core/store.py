"""DynamoDB single-table access.

Every key is built here so the layout stays in one place:

    PLAYER#<player_id>   PROFILE                the player record
    PLAYER#<player_id>   EVENT#<iso>#<rand>     timeline: every in/outbound event
    PLAYER#<player_id>   QUOTA#<yyyy-mm-dd>     daily send counter (ttl'd)
    CHAT#telegram#<id>   PLAYER                 inbound chat -> player lookup
    CODE#<enrolment>     PLAYER                 enrolment code -> player lookup

    gsi1: gsi1pk = STATUS#<status>, gsi1sk = <last_contact_iso>
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from backend.core import config
from backend.core.clock import iso, local_date_key, now_utc, ttl_epoch
from backend.core.ids import event_id
from backend.core.models import Channel, Direction, EventKind, Player, PlayerStatus

# Daily counters are only interesting for a couple of days after the fact.
QUOTA_TTL_DAYS = 3

# Long enough to outlive Telegram's retry window by a wide margin, short
# enough that these rows never accumulate.
UPDATE_TTL_DAYS = 2

_resource = None


def _table():
    global _resource
    if _resource is None:
        _resource = boto3.resource(
            "dynamodb", region_name=os.environ.get("AWS_REGION", config.REGION)
        )
    return _resource.Table(config.table_name())


def _clean(value: Any) -> Any:
    """DynamoDB rejects empty strings in keys and floats everywhere."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _undecimal(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: _undecimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_undecimal(v) for v in value]
    return value


# --------------------------------------------------------------------- keys


def player_pk(player_id: str) -> str:
    return f"PLAYER#{player_id}"


def chat_pk(channel: Channel | str, chat_id: int | str) -> str:
    return f"CHAT#{channel}#{chat_id}"


def code_pk(code: str) -> str:
    return f"CODE#{code.strip().upper()}"


# ------------------------------------------------------------------ players


def put_player(player: Player) -> Player:
    item = {
        "pk": player_pk(player.player_id),
        "sk": "PROFILE",
        "gsi1pk": f"STATUS#{player.status}",
        "gsi1sk": player.last_contact_at or player.created_at,
        **player.to_item(),
    }
    _table().put_item(Item=_clean(item))
    return player


def get_player(player_id: str) -> Player | None:
    resp = _table().get_item(Key={"pk": player_pk(player_id), "sk": "PROFILE"})
    item = resp.get("Item")
    return Player.from_item(_undecimal(item)) if item else None


def list_players(status: PlayerStatus | None = None) -> list[Player]:
    """Small scale by design -- the spec caps this at 10 players."""
    if status is not None:
        resp = _table().query(
            IndexName="gsi1",
            KeyConditionExpression=boto3.dynamodb.conditions.Key("gsi1pk").eq(
                f"STATUS#{status}"
            ),
        )
        items = resp.get("Items", [])
    else:
        items = [
            item
            for item in _scan_all()
            if item.get("sk") == "PROFILE"
            and str(item.get("pk", "")).startswith("PLAYER#")
        ]
    return [Player.from_item(_undecimal(i)) for i in items]


def _scan_all() -> Iterator[dict[str, Any]]:
    kwargs: dict[str, Any] = {}
    while True:
        resp = _table().scan(**kwargs)
        yield from resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            return
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def update_player_status(
    player_id: str, status: PlayerStatus, *, reason: str | None = None
) -> None:
    now = iso(now_utc())
    names = {"#s": "status", "#g": "gsi1pk"}
    values: dict[str, Any] = {":s": str(status), ":g": f"STATUS#{status}"}
    expr = "SET #s = :s, #g = :g"
    if status is PlayerStatus.STOPPED:
        expr += ", stopped_at = :t, stop_reason = :r"
        values[":t"] = now
        values[":r"] = reason or "player requested"
    _table().update_item(
        Key={"pk": player_pk(player_id), "sk": "PROFILE"},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def touch_last_contact(player_id: str, at: str | None = None) -> None:
    stamp = at or iso(now_utc())
    _table().update_item(
        Key={"pk": player_pk(player_id), "sk": "PROFILE"},
        UpdateExpression="SET last_contact_at = :t, gsi1sk = :t",
        ExpressionAttributeValues={":t": stamp},
    )


def delete_player(player_id: str) -> int:
    """Delete a player and every row belonging to them.

    Backs the spec's "one command deletes a player and all their data". Media
    objects in S3 are removed by the caller; this handles the table.
    """
    table = _table()
    deleted = 0
    player = get_player(player_id)

    resp = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq(
            player_pk(player_id)
        )
    )
    with table.batch_writer() as batch:
        for item in resp.get("Items", []):
            batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
            deleted += 1

    # Reverse-lookup rows live in their own partitions.
    if player:
        if player.telegram_chat_id is not None:
            table.delete_item(
                Key={
                    "pk": chat_pk(Channel.TELEGRAM, player.telegram_chat_id),
                    "sk": "PLAYER",
                }
            )
            deleted += 1
        if player.enrolment_code:
            table.delete_item(
                Key={"pk": code_pk(player.enrolment_code), "sk": "PLAYER"}
            )
            deleted += 1
    return deleted


# ------------------------------------------------------------ chat bindings


def bind_chat(channel: Channel | str, chat_id: int | str, player_id: str) -> None:
    _table().put_item(
        Item={
            "pk": chat_pk(channel, chat_id),
            "sk": "PLAYER",
            "player_id": player_id,
            "bound_at": iso(now_utc()),
        }
    )


def unbind_chat(player_id: str) -> None:
    """Detach a player from their channel without deleting them.

    Used when a chat is moved to a different player: the old player keeps their
    whole timeline but is no longer reachable, so nothing can be sent to a chat
    that now belongs to someone else.
    """
    _table().update_item(
        Key={"pk": player_pk(player_id), "sk": "PROFILE"},
        UpdateExpression="REMOVE telegram_chat_id",
    )


def player_id_for_chat(channel: Channel | str, chat_id: int | str) -> str | None:
    resp = _table().get_item(Key={"pk": chat_pk(channel, chat_id), "sk": "PLAYER"})
    item = resp.get("Item")
    return item.get("player_id") if item else None


def put_enrolment_code(code: str, player_id: str) -> None:
    _table().put_item(
        Item={"pk": code_pk(code), "sk": "PLAYER", "player_id": player_id}
    )


def player_id_for_code(code: str) -> str | None:
    resp = _table().get_item(Key={"pk": code_pk(code), "sk": "PLAYER"})
    item = resp.get("Item")
    return item.get("player_id") if item else None


def consume_enrolment_code(code: str) -> None:
    _table().delete_item(Key={"pk": code_pk(code), "sk": "PLAYER"})


# ----------------------------------------------------------------- timeline


def record_event(
    player_id: str,
    *,
    direction: Direction,
    channel: Channel,
    kind: EventKind,
    text: str | None = None,
    **meta: Any,
) -> str:
    """Append one row to the player's timeline. Never raises into the caller.

    The admin panel reads this partition directly; it is the single source of
    truth for what the player saw and said.
    """
    at = now_utc()
    sk = f"EVENT#{event_id(at)}"
    item = {
        "pk": player_pk(player_id),
        "sk": sk,
        "at": iso(at),
        "direction": str(direction),
        "channel": str(channel),
        "kind": str(kind),
        "text": text,
        **meta,
    }
    _table().put_item(Item=_clean({k: v for k, v in item.items() if v is not None}))
    return sk


def timeline(player_id: str, *, limit: int = 100, ascending: bool = True) -> list[dict]:
    resp = _table().query(
        KeyConditionExpression=(
            boto3.dynamodb.conditions.Key("pk").eq(player_pk(player_id))
            & boto3.dynamodb.conditions.Key("sk").begins_with("EVENT#")
        ),
        ScanIndexForward=ascending,
        Limit=limit,
    )
    return [_undecimal(i) for i in resp.get("Items", [])]


# -------------------------------------------------------------------- beats


def record_beat(player_id: str, beat_id: str, **fields: Any) -> None:
    """Upsert what happened during one beat.

    Separate from the timeline because it answers a different question. The
    timeline is "what was said"; this is "how far through the arc is this
    player, and did the day land". The admin panel reads both.
    """
    if not fields:
        return
    names = {f"#f{i}": k for i, k in enumerate(fields)}
    values = {f":v{i}": v for i, v in enumerate(fields.values())}
    assignments = ", ".join(f"{n} = {v}" for n, v in zip(names, values, strict=True))
    _table().update_item(
        Key={"pk": player_pk(player_id), "sk": f"BEAT#{beat_id}"},
        UpdateExpression=f"SET {assignments}",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=_clean(values),
    )


def get_beat(player_id: str, beat_id: str) -> dict | None:
    resp = _table().get_item(Key={"pk": player_pk(player_id), "sk": f"BEAT#{beat_id}"})
    item = resp.get("Item")
    return _undecimal(item) if item else None


def beats(player_id: str) -> list[dict]:
    resp = _table().query(
        KeyConditionExpression=(
            boto3.dynamodb.conditions.Key("pk").eq(player_pk(player_id))
            & boto3.dynamodb.conditions.Key("sk").begins_with("BEAT#")
        )
    )
    return [_undecimal(i) for i in resp.get("Items", [])]


# ------------------------------------------------------------- idempotency


class AlreadyHandled(Exception):
    """This inbound update has been processed before."""


def claim_update(channel: Channel | str, update_id: int | str) -> None:
    """Claim one inbound update, or raise if it was already claimed.

    Telegram retries any update it does not get a 2xx for, and generation
    turned a handler that answered in milliseconds into one that can take
    several seconds. A retry arriving mid-generation would otherwise produce a
    second reply to the same message and burn a second message from the
    player's daily six.
    """
    try:
        _table().put_item(
            Item={
                "pk": f"UPDATE#{channel}#{update_id}",
                "sk": "SEEN",
                "at": iso(now_utc()),
                "ttl": ttl_epoch(now_utc(), UPDATE_TTL_DAYS),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise AlreadyHandled(str(update_id)) from exc
        raise


# -------------------------------------------------------------- rate limits


class RateLimited(Exception):
    """Raised when a player has already had their allowance of story messages."""


def consume_daily_quota(player_id: str, timezone: str, *, limit: int) -> int:
    """Atomically claim one story message from today's allowance.

    Uses a conditional ADD so two concurrent Lambdas cannot both squeeze past
    the limit. Returns the new count; raises :class:`RateLimited` if the
    allowance is already spent.
    """
    now = now_utc()
    day = local_date_key(now, timezone)
    try:
        resp = _table().update_item(
            Key={"pk": player_pk(player_id), "sk": f"QUOTA#{day}"},
            UpdateExpression="SET #c = if_not_exists(#c, :zero) + :one, #t = :ttl",
            ConditionExpression="attribute_not_exists(#c) OR #c < :limit",
            ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
            ExpressionAttributeValues={
                ":zero": 0,
                ":one": 1,
                ":limit": limit,
                ":ttl": ttl_epoch(now, QUOTA_TTL_DAYS),
            },
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise RateLimited(
                f"{player_id} has spent today's {limit} messages"
            ) from exc
        raise
    return int(resp["Attributes"]["count"])


def quota_used(player_id: str, timezone: str) -> int:
    day = local_date_key(now_utc(), timezone)
    resp = _table().get_item(Key={"pk": player_pk(player_id), "sk": f"QUOTA#{day}"})
    item = resp.get("Item")
    return int(item["count"]) if item and "count" in item else 0
