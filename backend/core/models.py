"""Domain types.

Kept as plain dataclasses and str enums so they serialise into DynamoDB
without a mapping layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from backend.core.clock import iso, now_utc


class PlayerStatus(StrEnum):
    #: Enrolled, story may run.
    ACTIVE = "ACTIVE"
    #: Suspended by the player (PAUSE) or the operator. Resumable.
    PAUSED = "PAUSED"
    #: Halted by the player (STOP) or the operator. Terminal for the arc.
    STOPPED = "STOPPED"
    #: Created by the operator, waiting for the player to send their code.
    PENDING = "PENDING"


class Channel(StrEnum):
    TELEGRAM = "telegram"
    EMAIL = "email"
    VOICE = "voice"
    ADMIN = "admin"


class Direction(StrEnum):
    IN = "in"
    OUT = "out"


class EventKind(StrEnum):
    MESSAGE = "message"
    PHOTO = "photo"
    LOCATION = "location"
    COMMAND = "command"
    #: An out-of-character system reply (STOP confirmation, /real, errors).
    SYSTEM = "system"
    #: An outbound message that was refused before dispatch.
    BLOCKED = "blocked"
    ERROR = "error"


class MessageKind(StrEnum):
    """What kind of outbound message this is. Decides which gates apply.

    ``STORY`` is in-character narrative. It is subject to every gate: player
    status, quiet hours, and the daily rate limit.

    ``SYSTEM_REPLY`` is an out-of-character reply to something the player just
    sent -- a STOP confirmation, ``/real``, a PAUSE acknowledgement. It bypasses
    quiet hours and the rate limit *by design*: the spec requires STOP to work
    "at any point", and a player who texts at 23:00 is demonstrably awake.
    Silencing a safety confirmation to honour quiet hours would be the wrong
    reading of both rules.
    """

    STORY = "story"
    SYSTEM_REPLY = "system_reply"


class BlockReason(StrEnum):
    QUIET_HOURS = "quiet_hours"
    RATE_LIMITED = "rate_limited"
    PLAYER_STOPPED = "player_stopped"
    PLAYER_PAUSED = "player_paused"
    NO_CHANNEL = "no_channel"
    VALIDATION_FAILED = "validation_failed"


@dataclass
class Player:
    player_id: str
    display_name: str
    timezone: str = "UTC"
    status: PlayerStatus = PlayerStatus.PENDING
    arc_id: str | None = None
    #: When the arc began, in UTC. The current beat is derived from this and
    #: the player's timezone, so it is set once, at enrolment, and not moved.
    arc_started_at: str | None = None
    telegram_chat_id: int | None = None
    enrolment_code: str | None = None
    created_at: str = field(default_factory=lambda: iso(now_utc()))
    last_contact_at: str | None = None
    #: Set when the player stops, for the admin timeline.
    stopped_at: str | None = None
    stop_reason: str | None = None
    #: Number of voice calls placed this arc. Capped at MAX_CALLS_PER_ARC.
    calls_placed: int = 0
    notes: str | None = None

    @property
    def is_stopped(self) -> bool:
        return self.status is PlayerStatus.STOPPED

    @property
    def accepts_story(self) -> bool:
        return self.status is PlayerStatus.ACTIVE

    def to_item(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if v is not None}
        data["status"] = str(self.status)
        return data

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Player:
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in item.items() if k in known}
        if "telegram_chat_id" in kwargs and kwargs["telegram_chat_id"] is not None:
            kwargs["telegram_chat_id"] = int(kwargs["telegram_chat_id"])
        if "calls_placed" in kwargs:
            kwargs["calls_placed"] = int(kwargs["calls_placed"])
        kwargs["status"] = PlayerStatus(kwargs.get("status", PlayerStatus.PENDING))
        return cls(**kwargs)


@dataclass
class SendResult:
    """The outcome of an attempted outbound message.

    ``sent`` false is not an error -- it is the safety layer doing its job. The
    caller decides whether to reschedule (quiet hours) or drop (stopped).
    """

    sent: bool
    reason: BlockReason | None = None
    #: For quiet-hours blocks: when this message could be sent instead.
    retry_at: str | None = None
    message_id: str | None = None
    text: str | None = None

    @property
    def blocked(self) -> bool:
        return not self.sent
