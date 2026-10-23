from __future__ import annotations

import io
import urllib.error
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aegis.core.secrets import MissingSecret, get_secret
from aegis.telegram.bot_api import ALLOWED_UPDATES, BotApi, TelegramApiError, make_transport
from aegis.telegram.mapper import IntakeReject, TelegramIntakeConfig, map_update
from aegis.telegram.poller import OffsetStore, TelegramPoller
from aegis.trading.risk import Quote
from tests.conftest import NOW, SOURCE

CHANNEL = SOURCE  # trusted signal channel the bot is a member of
GROUP = -1009876543210  # owner's private intake group
OWNER = 111
STRANGER = 222
TS = int(NOW.timestamp())

CFG = TelegramIntakeConfig(intake_chats=frozenset({CHANNEL, GROUP}), trusted_users=frozenset({OWNER}))
SIGNAL = "BUY EURUSD 1.0851 SL 1.0820 TP 1.0900"


def channel_post(update_id: int, message_id: int, text: str = SIGNAL, chat: int = CHANNEL, date: int = TS) -> dict:
    return {
        "update_id": update_id,
        "channel_post": {"message_id": message_id, "date": date, "chat": {"id": chat, "type": "channel"}, "text": text},
    }


def group_msg(
    update_id: int, message_id: int, sender: int = OWNER, text: str = SIGNAL, origin: dict | None = None
) -> dict:
    msg: dict[str, Any] = {
        "message_id": message_id,
        "date": TS,
        "chat": {"id": GROUP, "type": "supergroup"},
        "from": {"id": sender, "is_bot": False},
        "text": text,
    }
    if origin:
        msg["forward_origin"] = origin
    return {"update_id": update_id, "message": msg}


def channel_origin(message_id: int, date: int = TS, chat: int = CHANNEL) -> dict:
    return {"type": "channel", "date": date, "chat": {"id": chat, "type": "channel"}, "message_id": message_id}


# --- mapper -------------------------------------------------------------------------------------


def test_channel_post_maps_to_channel_source():
    m = map_update(channel_post(1, 50), CFG)
    assert m.message is not None and m.message.source_chat_id == CHANNEL and m.message.message_id == 50


def test_forward_uses_original_channel_id_and_time():
    old = TS - 3600
    m = map_update(group_msg(1, 7, origin=channel_origin(50, date=old)), CFG)
    assert m.message is not None
    assert (m.message.source_chat_id, m.message.message_id) == (CHANNEL, 50)
    assert m.message.posted_at == datetime.fromtimestamp(old, UTC)  # staleness judged on original post time


def test_typed_message_from_owner_in_private_group():
    m = map_update(group_msg(1, 8), CFG)
    assert m.message is not None and m.message.source_chat_id == GROUP


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        (group_msg(1, 9, sender=STRANGER), IntakeReject.UNTRUSTED_SENDER),
        (channel_post(1, 9, chat=-100555), IntakeReject.UNKNOWN_INTAKE_CHAT),
        (
            group_msg(1, 9, origin={"type": "user", "date": TS, "sender_user": {"id": 5}}),
            IntakeReject.UNATTRIBUTABLE_FORWARD,
        ),
        (
            group_msg(1, 9, origin={"type": "hidden_user", "date": TS, "sender_user_name": "x"}),
            IntakeReject.UNATTRIBUTABLE_FORWARD,
        ),
        ({"update_id": 1, "edited_channel_post": channel_post(0, 50)["channel_post"]}, IntakeReject.EDITED),
        ({"update_id": 1, "edited_message": group_msg(0, 8)["message"]}, IntakeReject.EDITED),
        ({"update_id": 1, "callback_query": {}}, IntakeReject.UNSUPPORTED_UPDATE),
        (channel_post(1, 9, text=""), IntakeReject.NO_TEXT),
    ],
)
def test_mapper_rejections(update, reason):
    assert map_update(update, CFG).reject is reason


def test_caption_is_used_for_media_posts():
    upd = channel_post(1, 51)
    upd["channel_post"].pop("text")
    upd["channel_post"]["caption"] = SIGNAL
    assert map_update(upd, CFG).message is not None


# --- poller + pipeline ----------------------------------------------------------------------------


class FakeApi(BotApi):
    def __init__(self, batches: list[list[dict]]) -> None:
        self.batches = batches
        self.offsets: list[int | None] = []

    def get_updates(self, offset: int | None, timeout: int = 25, limit: int = 100) -> list[dict]:
        self.offsets.append(offset)
        return self.batches.pop(0) if self.batches else []


def _poller(stack, api, path=None):
    stack.validator._sources = frozenset({CHANNEL, GROUP})
    return TelegramPoller(api, CFG, stack.agent, OffsetStore(path), stack.audit, clock=lambda: NOW)


def test_end_to_end_bot_to_paper_trade(stack, tmp_path):
    api = FakeApi(
        [
            [
                channel_post(10, 50),  # executes
                group_msg(11, 7, origin=channel_origin(50)),  # same post forwarded -> duplicate
                group_msg(12, 8, sender=STRANGER, text="SELL GBPUSD 1.3000 SL 1.3030"),  # untrusted sender
                {
                    "update_id": 13,
                    "edited_channel_post": channel_post(0, 50, text="BUY EURUSD 1.0851 SL 1.0700")["channel_post"],
                },
            ]
        ]
    )
    poller = _poller(stack, api, tmp_path / "offset.json")
    outcomes = poller.poll_once()
    assert [(o.stage, o.accepted) for o in outcomes] == [("executed", True), ("validation", False)]
    assert "duplicate" in outcomes[1].reasons
    assert len(stack.broker.positions()) == 1
    rejected = [r.payload["reason"] for r in stack.audit.records("telegram.update_rejected")]
    assert rejected == ["untrusted_sender", "edited_message"]
    assert OffsetStore(tmp_path / "offset.json").offset == 14
    poller.poll_once()
    assert api.offsets == [None, 14]
    assert stack.audit.verify()[0]


def test_stale_forward_is_rejected(stack):
    api = FakeApi([[group_msg(1, 7, origin=channel_origin(77, date=TS - 3600))]])
    out = _poller(stack, api).poll_once()
    assert out[0].stage == "validation" and "stale" in out[0].reasons


def test_offset_committed_before_handling_at_most_once(stack, tmp_path):
    class Boom(Exception):
        pass

    def explode(*a, **k):
        raise Boom

    api = FakeApi([[channel_post(20, 60)]])
    poller = _poller(stack, api, tmp_path / "o.json")
    stack.agent.handle = explode  # crash mid-handling
    with pytest.raises(Boom):
        poller.poll_once()
    assert OffsetStore(tmp_path / "o.json").offset == 21  # will not be redelivered after restart


# --- transport / secrets ------------------------------------------------------------------------


def test_transport_errors_never_contain_token(monkeypatch):
    token = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"ok":false}'))

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(TelegramApiError) as exc:
        make_transport(token)("getMe", {})
    assert token not in str(exc.value) and exc.value.__cause__ is None


def test_get_updates_requests_only_message_types():
    seen = {}

    def transport(method, params):
        seen.update(method=method, params=params)
        return []

    BotApi(transport).get_updates(offset=5, timeout=1)
    assert seen["method"] == "getUpdates" and seen["params"]["allowed_updates"] == ALLOWED_UPDATES
    assert seen["params"]["offset"] == 5


def test_get_secret_env_fallback(monkeypatch):
    monkeypatch.setenv("AEGIS_TEST_SECRET", "s3cret")
    assert get_secret("AEGIS_TEST_SECRET") == "s3cret"
    monkeypatch.delenv("AEGIS_TEST_SECRET")
    with pytest.raises(MissingSecret):
        get_secret("AEGIS_TEST_SECRET")


def test_paper_broker_pulls_live_prices_from_feed(stack):
    from aegis.trading.brokers.paper import PaperBroker

    calls = []

    def feed(symbol: str) -> Quote:
        calls.append(symbol)
        return Quote(symbol=symbol, bid=1.1000, ask=1.1001, ts=NOW + timedelta(seconds=len(calls)))

    broker = PaperBroker(stack.signer, stack.audit, quote_feed=feed)
    assert broker.quote("EURUSD").ask == 1.1001 and calls == ["EURUSD"]
