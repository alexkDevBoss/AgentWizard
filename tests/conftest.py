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


class FakeModel:
    """Stands in for Bedrock. Records every call; never touches the network.

    Installed for the whole suite by an autouse fixture rather than per test,
    so a test that forgets to ask for it fails loudly instead of quietly
    billing a real model.
    """

    def __init__(self) -> None:
        self.text = "The workshop is freezing and I have found something odd."
        self.error: Exception | None = None
        self.review_error: Exception | None = None
        self.verdict: dict = {"safe": True, "rules": [], "reason": "ok"}
        self.writes: list[dict] = []
        self.reviews: list[dict] = []

    def write(self, *, system, messages, **_kwargs) -> str:
        self.writes.append({"system": system, "messages": messages})
        if self.error:
            raise self.error
        return self.text

    def judge(self, *, system, messages, tool, **_kwargs) -> dict:
        self.reviews.append({"system": system, "messages": messages, "tool": tool})
        if self.review_error:
            raise self.review_error
        return self.verdict

    # convenience -----------------------------------------------------------

    def refuse(self, *rules: str, reason: str = "unsafe") -> None:
        self.verdict = {"safe": False, "rules": list(rules), "reason": reason}

    @property
    def last_system(self) -> str:
        return self.writes[-1]["system"]

    @property
    def last_messages(self) -> list[dict]:
        return self.writes[-1]["messages"]


@pytest.fixture(autouse=True)
def story(monkeypatch) -> FakeModel:
    """Every test runs against a fake model. No test may reach Bedrock."""
    from backend.core import model

    fake = FakeModel()
    monkeypatch.setattr(model, "write", fake.write)
    monkeypatch.setattr(model, "judge", fake.judge)

    def forbidden():
        raise AssertionError("a test tried to build a real Bedrock client")

    monkeypatch.setattr(model, "client", forbidden)
    return fake


@pytest.fixture
def frozen(monkeypatch):
    """Pin every clock in the enforcement path. Returns a setter.

    Both modules are patched, not just dispatch: the engine derives the current
    beat from the same instant that dispatch checks quiet hours against, and a
    test where those two disagree is testing a situation that cannot happen.
    """
    from backend.core import dispatch, engine

    state = {"now": datetime(2026, 3, 10, 15, 0, tzinfo=UTC)}

    def set_now(dt: datetime) -> None:
        state["now"] = dt

    monkeypatch.setattr(dispatch, "now_utc", lambda: state["now"])
    monkeypatch.setattr(engine, "now_utc", lambda: state["now"])
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
