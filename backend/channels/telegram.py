"""Telegram Bot API client.

Built on the standard library on purpose: the only thing we need is JSON over
HTTPS, and every third-party HTTP client would add megabytes to a Lambda
bundle that currently has almost nothing in it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from backend.core import logs

API_ROOT = "https://api.telegram.org"
DEFAULT_TIMEOUT_S = 10


class TelegramError(RuntimeError):
    """A non-ok response from the Bot API."""

    def __init__(self, method: str, code: int, description: str) -> None:
        super().__init__(f"{method} failed ({code}): {description}")
        self.method = method
        self.code = code
        self.description = description


class TelegramClient:
    def __init__(self, bot_token: str, *, timeout: int = DEFAULT_TIMEOUT_S) -> None:
        if not bot_token:
            raise ValueError("telegram bot token is empty -- fill in the secret")
        self._token = bot_token
        self._timeout = timeout

    # ------------------------------------------------------------- transport

    def _call(self, method: str, **params: Any) -> Any:
        url = f"{API_ROOT}/bot{self._token}/{method}"
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with logs.timed("telegram.api", method=method):
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                try:
                    description = json.loads(detail).get("description", detail)
                except json.JSONDecodeError:
                    description = detail
                raise TelegramError(method, exc.code, description) from exc

        if not payload.get("ok"):
            raise TelegramError(
                method, payload.get("error_code", 0), payload.get("description", "")
            )
        return payload.get("result")

    # --------------------------------------------------------------- methods

    def get_me(self) -> dict:
        return self._call("getMe")

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        """Send text. No parse mode by default.

        Markdown parsing is off deliberately: an unbalanced ``*`` or ``_`` in
        generated prose makes the Bot API reject the whole message, which would
        turn a cosmetic problem into a missed story beat.
        """
        return self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            link_preview_options={"is_disabled": True},
        )

    def send_chat_action(self, chat_id: int | str, action: str = "typing") -> bool:
        return self._call("sendChatAction", chat_id=chat_id, action=action)

    def get_file(self, file_id: str) -> dict:
        return self._call("getFile", file_id=file_id)

    def download(self, file_id: str, *, max_bytes: int = 5_000_000) -> bytes:
        """Fetch a photo's bytes. Two round trips: getFile, then the CDN.

        Reads at most ``max_bytes``. The Bot API caps photos well below this,
        so a larger response means something is wrong and reading it all would
        only turn that into a memory problem inside a Lambda.
        """
        path = self.get_file(file_id).get("file_path")
        if not path:
            raise TelegramError("getFile", 0, f"no file_path for {file_id}")

        request = urllib.request.Request(self.file_url(path), method="GET")
        with (
            logs.timed("telegram.download", file_id=file_id),
            urllib.request.urlopen(request, timeout=self._timeout) as resp,
        ):
            return resp.read(max_bytes)

    def file_url(self, file_path: str) -> str:
        return f"{API_ROOT}/file/bot{self._token}/{file_path}"

    # --------------------------------------------------------------- webhook

    def set_webhook(
        self,
        url: str,
        *,
        secret_token: str,
        allowed_updates: list[str] | None = None,
        drop_pending_updates: bool = True,
    ) -> bool:
        return self._call(
            "setWebhook",
            url=url,
            secret_token=secret_token,
            allowed_updates=allowed_updates or ["message", "edited_message"],
            drop_pending_updates=drop_pending_updates,
            max_connections=10,
        )

    def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        return self._call("deleteWebhook", drop_pending_updates=drop_pending_updates)

    def get_webhook_info(self) -> dict:
        return self._call("getWebhookInfo")


# ------------------------------------------------------------ update parsing


def extract_message(update: dict) -> dict | None:
    """Pull the message out of an update, whether new or edited."""
    return update.get("message") or update.get("edited_message")


def extract_location(update: dict) -> tuple[float, float] | None:
    """The raw coordinates from a location update, or None.

    Deliberately separate from :func:`describe_update`. That function's result
    is what gets written to the timeline, and it must never carry a position --
    so the coordinates are reachable only by asking for them explicitly, by a
    caller that intends to compare them against a geofence and drop them.
    """
    message = extract_message(update) or {}
    location = message.get("location") or {}
    lat, lon = location.get("latitude"), location.get("longitude")
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def describe_update(update: dict) -> dict[str, Any]:
    """Flatten an update into the fields we actually store.

    Note what is *not* returned: for a location update the coordinates are
    dropped here, at the edge. GPS never enters the system.
    """
    message = extract_message(update) or {}
    chat = message.get("chat") or {}
    result: dict[str, Any] = {
        "update_id": update.get("update_id"),
        "message_id": message.get("message_id"),
        "chat_id": chat.get("id"),
        "chat_type": chat.get("type"),
        "text": message.get("text") or message.get("caption"),
        "has_photo": bool(message.get("photo")),
        "has_location": bool(message.get("location")),
        "date": message.get("date"),
    }
    if result["has_photo"]:
        # Telegram sends every size; the last is the largest.
        result["photo_file_id"] = message["photo"][-1].get("file_id")
    return result
