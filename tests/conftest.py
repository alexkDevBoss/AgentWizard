"""Shared fixtures.

Every test runs against a moto-backed DynamoDB table with the same key schema
as the deployed one, and a fake Telegram client that records sends instead of
making them. Nothing here touches the network or real AWS.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

# Must be set before anything under backend.core reads them.
os.environ.setdefault("ADVENTURE_TABLE_NAME", "adventure-agent-test")
os.environ.setdefault("ADVENTURE_SECRET_NAME", "adventure-agent/test")
os.environ.setdefault("ADVENTURE_ENV", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

TABLE_NAME = os.environ["ADVENTURE_TABLE_NAME"]


@pytest.fixture
def table(monkeypatch):
    """A fresh moto DynamoDB table matching the CDK definition."""
    import boto3
    from moto import mock_aws

    from backend.core import store

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=TABLE_NAME,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "gsi1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        # The lazily-built resource must not survive between mock contexts.
        monkeypatch.setattr(store, "_resource", None)
        yield client
        monkeypatch.setattr(store, "_resource", None)


class FakeTelegram:
    """Records what would have been sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[int | str, str]] = []
        self.actions: list[tuple[int | str, str]] = []
        self.next_message_id = 1000

    def send_message(self, chat_id, text, **_kwargs):
        self.sent.append((chat_id, text))
        self.next_message_id += 1
        return {"message_id": self.next_message_id}

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))
        return True

    # convenience -----------------------------------------------------------

    @property
    def texts(self) -> list[str]:
        return [text for _chat, text in self.sent]

    @property
    def last(self) -> str | None:
        return self.texts[-1] if self.sent else None


@pytest.fixture
def telegram(monkeypatch) -> FakeTelegram:
    from backend.core import dispatch

    fake = FakeTelegram()
    monkeypatch.setattr(dispatch, "telegram", lambda: fake)
    # The randomised "typing" pause is a product feature, not a test one.
    monkeypatch.setattr(dispatch.time, "sleep", lambda _s: None)
    return fake


@pytest.fixture
def frozen(monkeypatch):
    """Pin the clock that dispatch reads. Returns a setter."""
    from backend.core import dispatch

    state = {"now": datetime(2026, 3, 10, 15, 0, tzinfo=UTC)}

    def set_now(dt: datetime) -> None:
        state["now"] = dt

    monkeypatch.setattr(dispatch, "now_utc", lambda: state["now"])
    return set_now


@pytest.fixture
def player(table):
    """An ACTIVE player with a bound Telegram chat, persisted."""
    from backend.core import store
    from backend.core.models import Channel, Player, PlayerStatus

    p = Player(
        player_id="plr_test000001",
        display_name="Test Player",
        timezone="Europe/London",
        status=PlayerStatus.ACTIVE,
        telegram_chat_id=42,
    )
    store.put_player(p)
    store.bind_chat(Channel.TELEGRAM, 42, p.player_id)
    return p
