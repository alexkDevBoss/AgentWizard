"""The Telegram webhook Lambda, end to end.

Drives the real handler with real API Gateway v2 payloads. Only two things are
faked: the Bot API transport and the Secrets Manager lookup.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime

import pytest

from backend.core import safety, store
from backend.core.models import Channel, Player, PlayerStatus
from backend.handlers import telegram_webhook as webhook

DAYTIME = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
SECRET = "test-webhook-secret"


@pytest.fixture(autouse=True)
def webhook_secret(monkeypatch):
    from backend.core import secrets

    monkeypatch.setattr(secrets, "telegram_webhook_secret", lambda: SECRET)
    monkeypatch.setattr(secrets, "telegram_bot_token", lambda: "fake:token")


#: Telegram never reuses an update_id, and the handler now depends on that to
#: drop retries -- so the helper must not reuse one either.
_update_ids = itertools.count(1)


def update(
    text: str | None = None,
    *,
    chat_id: int = 42,
    update_id: int | None = None,
    **message_extra,
) -> dict:
    message = {
        "message_id": 7,
        "date": 1772000000,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "is_bot": False},
        **message_extra,
    }
    if text is not None:
        message["text"] = text
    return {
        "update_id": next(_update_ids) if update_id is None else update_id,
        "message": message,
    }


def request(body: dict, *, secret: str | None = SECRET) -> dict:
    headers = {"content-type": "application/json"}
    if secret is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    return {
        "headers": headers,
        "body": json.dumps(body),
        "requestContext": {"http": {"method": "POST", "sourceIp": "149.154.167.99"}},
    }


# ------------------------------------------------------------- security


def test_a_request_without_the_secret_header_is_rejected(table, telegram) -> None:
    response = webhook.handler(request(update("hello"), secret=None))
    assert response["statusCode"] == 401
    assert telegram.sent == []


def test_a_request_with_the_wrong_secret_is_rejected(table, telegram) -> None:
    response = webhook.handler(request(update("hello"), secret="not-it"))
    assert response["statusCode"] == 401
    assert telegram.sent == []


# ------------------------------------------------------------ enrolment


def test_an_unenrolled_chat_is_told_what_this_is(table, telegram, frozen) -> None:
    frozen(DAYTIME)
    response = webhook.handler(request(update("hello?")))

    assert response["statusCode"] == 200
    assert telegram.last == safety.UNKNOWN_CHAT


def test_a_valid_code_binds_the_chat_and_activates_the_player(
    table, telegram, frozen
) -> None:
    frozen(DAYTIME)
    pending = Player(
        player_id="plr_pending0001",
        display_name="Invitee",
        timezone="Europe/London",
        status=PlayerStatus.PENDING,
        enrolment_code="AB3KD9XY",
    )
    store.put_player(pending)
    store.put_enrolment_code("AB3KD9XY", pending.player_id)

    webhook.handler(request(update("/start AB3KD9XY", chat_id=99)))

    enrolled = store.get_player(pending.player_id)
    assert enrolled.status is PlayerStatus.ACTIVE
    assert enrolled.telegram_chat_id == 99
    assert store.player_id_for_chat(Channel.TELEGRAM, 99) == pending.player_id
    assert telegram.last == safety.ENROLLED
    # The code is single-use.
    assert store.player_id_for_code("AB3KD9XY") is None


def test_an_unknown_code_is_refused(table, telegram, frozen) -> None:
    frozen(DAYTIME)
    webhook.handler(request(update("/start NOPE1234", chat_id=99)))

    assert telegram.last == safety.BAD_CODE
    assert store.player_id_for_chat(Channel.TELEGRAM, 99) is None


def test_enrolment_is_not_self_signup(table, telegram, frozen) -> None:
    """A bare /start with no operator-issued code enrols nobody."""
    frozen(DAYTIME)
    webhook.handler(request(update("/start", chat_id=99)))

    assert store.player_id_for_chat(Channel.TELEGRAM, 99) is None
    assert telegram.last == safety.UNKNOWN_CHAT


# -------------------------------------------------------- the safety commands


def test_stop_from_the_player_halts_everything(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    webhook.handler(request(update("STOP")))

    assert store.get_player(player.player_id).status is PlayerStatus.STOPPED
    assert telegram.last == safety.STOP_CONFIRMATION


@pytest.mark.parametrize("text", ["stop", "Stop.", "please stop", "/stop", "STOP!"])
def test_stop_is_honoured_however_it_is_written(player, telegram, frozen, text) -> None:
    frozen(DAYTIME)
    webhook.handler(request(update(text)))
    assert store.get_player(player.player_id).status is PlayerStatus.STOPPED


def test_pause_and_resume_round_trip(player, telegram, frozen) -> None:
    frozen(DAYTIME)

    webhook.handler(request(update("PAUSE")))
    assert store.get_player(player.player_id).status is PlayerStatus.PAUSED

    webhook.handler(request(update("RESUME")))
    assert store.get_player(player.player_id).status is PlayerStatus.ACTIVE


def test_real_always_returns_the_plain_statement(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    webhook.handler(request(update("/real")))

    assert telegram.last == safety.REAL_TEXT
    assert "fiction" in telegram.last.lower()
    assert "STOP" in telegram.last


def test_real_works_after_the_daily_allowance_is_spent(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    for _ in range(8):
        webhook.handler(request(update("hello")))
    telegram.sent.clear()

    webhook.handler(request(update("/real")))
    assert telegram.last == safety.REAL_TEXT


def test_real_works_after_stopping(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    webhook.handler(request(update("STOP")))
    telegram.sent.clear()

    webhook.handler(request(update("/real")))
    assert telegram.last == safety.REAL_TEXT


# ------------------------------------------------------------- ordinary traffic


def test_an_ordinary_message_gets_a_generated_reply(
    player, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    story.text = "Wren here. The bench by the shop? That is the one."
    webhook.handler(request(update("found the bench")))

    assert story.text in telegram.last
    # What the player said reaches the model as the last turn of the
    # conversation, not as a template the reply is built from.
    assert story.last_messages[-1] == {"role": "user", "content": "found the bench"}


def test_inbound_and_outbound_are_both_on_the_timeline(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    webhook.handler(request(update("found the bench")))

    events = store.timeline(player.player_id)
    assert [e["direction"] for e in events] == ["in", "out"]


def test_a_retried_update_is_not_answered_twice(
    player, telegram, frozen, story
) -> None:
    """Telegram retries anything it does not get a 2xx for.

    Generation made this handler slow enough for that to matter: a retry
    arriving mid-generation would reply twice to one message and spend two of
    the player's six on it.
    """
    frozen(DAYTIME)
    retried = update("found the bench", update_id=9001)

    webhook.handler(request(retried))
    webhook.handler(request(retried))

    assert len(telegram.sent) == 1
    inbound = [e for e in store.timeline(player.player_id) if e["direction"] == "in"]
    assert len(inbound) == 1


def test_two_different_updates_are_both_answered(
    player, telegram, frozen, story
) -> None:
    frozen(DAYTIME)
    webhook.handler(request(update("one", update_id=9101)))
    webhook.handler(request(update("two", update_id=9102)))

    assert len(telegram.sent) == 2


def test_a_photo_is_recorded_with_its_file_id(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    webhook.handler(
        request(update(photo=[{"file_id": "small"}, {"file_id": "largest"}]))
    )

    inbound = store.timeline(player.player_id)[0]
    assert inbound["kind"] == "photo"
    assert inbound["photo_file_id"] == "largest"


def test_gps_coordinates_are_never_stored(player, telegram, frozen) -> None:
    """The spec forbids persisting coordinates. They are dropped at the edge."""
    frozen(DAYTIME)
    webhook.handler(
        request(update(location={"latitude": 51.5072, "longitude": -0.1276}))
    )

    inbound = store.timeline(player.player_id)[0]
    assert inbound["kind"] == "location"
    serialised = json.dumps(inbound)
    assert "51.5" not in serialised
    assert "latitude" not in serialised


def test_a_distressed_message_is_flagged_but_does_not_auto_stop(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    webhook.handler(request(update("wait, is this real? I'm scared")))

    assert store.get_player(player.player_id).status is PlayerStatus.ACTIVE
    inbound = store.timeline(player.player_id)[0]
    assert inbound["needs_review"] is True


# ---------------------------------------------------------------- robustness


def test_malformed_json_does_not_trigger_a_telegram_retry(table, telegram) -> None:
    response = webhook.handler(
        {"headers": {"x-telegram-bot-api-secret-token": SECRET}, "body": "{not json"}
    )
    assert response["statusCode"] == 200


def test_an_update_with_no_message_is_ignored(table, telegram) -> None:
    response = webhook.handler(request({"update_id": 1}))
    assert response["statusCode"] == 200
    assert telegram.sent == []


def test_a_handler_failure_still_answers_200(
    player, telegram, frozen, monkeypatch
) -> None:
    """A retry storm against a broken handler is worse than one lost update."""
    frozen(DAYTIME)
    monkeypatch.setattr(
        store,
        "player_id_for_chat",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    response = webhook.handler(request(update("hello")))
    assert response["statusCode"] == 200


# ------------------------------------------------------------ re-enrolment


def make_pending(player_id: str, code: str) -> Player:
    pending = Player(
        player_id=player_id,
        display_name="Second Arc",
        timezone="Europe/London",
        status=PlayerStatus.PENDING,
        enrolment_code=code,
    )
    store.put_player(pending)
    store.put_enrolment_code(code, player_id)
    return pending


def test_a_bound_chat_can_be_moved_to_a_new_player(player, telegram, frozen) -> None:
    """Otherwise a chat is stuck on its first player and can never run a second arc."""
    frozen(DAYTIME)
    make_pending("plr_second00001", "NEWARC01")

    webhook.handler(request(update("/start NEWARC01")))

    assert store.player_id_for_chat(Channel.TELEGRAM, 42) == "plr_second00001"
    assert store.get_player("plr_second00001").status is PlayerStatus.ACTIVE
    assert store.get_player("plr_second00001").telegram_chat_id == 42
    assert telegram.last == safety.ENROLLED


def test_the_previous_player_becomes_unreachable_not_deleted(
    player, telegram, frozen
) -> None:
    frozen(DAYTIME)
    make_pending("plr_second00001", "NEWARC01")

    webhook.handler(request(update("/start NEWARC01")))

    previous = store.get_player(player.player_id)
    assert previous is not None  # their history is kept
    assert previous.telegram_chat_id is None
    assert any(
        "no longer reachable" in (e.get("text") or "")
        for e in store.timeline(player.player_id)
    )


def test_a_bad_code_does_not_move_a_bound_chat(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    webhook.handler(request(update("/start NOTACODE")))

    assert store.player_id_for_chat(Channel.TELEGRAM, 42) == player.player_id
    assert store.get_player(player.player_id).telegram_chat_id == 42


def test_a_code_for_the_same_player_is_not_a_rebind(player, telegram, frozen) -> None:
    frozen(DAYTIME)
    store.put_enrolment_code("SAMEONE1", player.player_id)

    webhook.handler(request(update("/start SAMEONE1")))

    assert store.get_player(player.player_id).telegram_chat_id == 42
    assert telegram.last == safety.REAL_TEXT
