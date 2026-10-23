"""Minimal Telegram Bot API client (stdlib only). Read-side methods only: getMe, getWebhookInfo, getUpdates.

The token is part of the request URL, so errors are re-raised without the URL to keep it out of logs.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

API_HOST = "https://api.telegram.org"
ALLOWED_UPDATES = ["message", "edited_message", "channel_post", "edited_channel_post"]
MAX_BYTES = 4 * 1024 * 1024

# (method, params) -> decoded "result". Injected so tests run offline.
Transport = Callable[[str, dict[str, Any]], Any]


class TelegramApiError(Exception):
    pass


def make_transport(token: str, timeout_s: float = 35.0) -> Transport:
    def call(method: str, params: dict[str, Any]) -> Any:
        body = json.dumps(params).encode()
        req = urllib.request.Request(  # noqa: S310 - fixed https host
            f"{API_HOST}/bot{token}/{method}", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
                raw = resp.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode(errors="replace")
            raise TelegramApiError(f"{method}: HTTP {exc.code} {detail}") from None
        except urllib.error.URLError as exc:
            raise TelegramApiError(f"{method}: network error {exc.reason}") from None
        if len(raw) > MAX_BYTES:
            raise TelegramApiError(f"{method}: response too large")
        data = json.loads(raw)
        if not data.get("ok"):
            raise TelegramApiError(f"{method}: {data.get('error_code')} {data.get('description')}")
        return data["result"]

    return call


class BotApi:
    def __init__(self, transport: Transport) -> None:
        self._call = transport

    def get_me(self) -> dict[str, Any]:
        result: dict[str, Any] = self._call("getMe", {})
        return result

    def get_webhook_info(self) -> dict[str, Any]:
        result: dict[str, Any] = self._call("getWebhookInfo", {})
        return result

    def get_updates(self, offset: int | None, timeout: int = 25, limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "limit": limit, "allowed_updates": ALLOWED_UPDATES}
        if offset is not None:
            params["offset"] = offset
        result: list[dict[str, Any]] = self._call("getUpdates", params)
        return result
