"""Identifier generation.

Event ids must sort by time inside a DynamoDB partition, so they are built as
``<iso timestamp>#<sequence>#<random>``. That keeps a player's timeline in
order under a plain Query with no index and no sorting on the client.
"""

from __future__ import annotations

import itertools
import secrets
import string
from datetime import datetime

from backend.core.clock import iso

_ALPHABET = string.ascii_lowercase + string.digits

#: Breaks ties between events created in the same microsecond by one process --
#: which is exactly the common case, an inbound message and its reply.
_sequence = itertools.count()
# Unambiguous subset for anything a human has to read out or retype.
_HUMAN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def player_id() -> str:
    return "plr_" + "".join(secrets.choice(_ALPHABET) for _ in range(12))


def event_id(at: datetime) -> str:
    """Time-sortable within a partition.

    ``<timestamp>#<sequence>#<random>``. The sequence guarantees correct order
    for events written by a single handler invocation; the random tail keeps
    two concurrent Lambdas from colliding on the same key.
    """
    seq = next(_sequence) % 1_000_000
    tail = "".join(secrets.choice(_ALPHABET) for _ in range(6))
    return f"{iso(at)}#{seq:06d}#{tail}"


def enrolment_code() -> str:
    """A short code the operator gives a player to bind their Telegram chat.

    Deliberately not self-signup: the operator mints this out of band. Short
    enough to say out loud, from an alphabet with no 0/O or 1/I confusion.
    """
    return "".join(secrets.choice(_HUMAN_ALPHABET) for _ in range(8))
