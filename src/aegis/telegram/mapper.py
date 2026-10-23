"""Map raw Bot API updates to RawMessage, enforcing *where* a message came from.

Three accepted delivery paths:
1. channel_post in a channel the bot is a member of        -> source = that channel
2. a trusted user forwards a channel post into a private
   group (or DM to the bot)                                -> source = original channel (forward_origin),
                                                              posted_at = original post time
3. a trusted user types a signal in a private intake group -> source = that group

Everything else is rejected: unknown chats, untrusted senders, forwards from users or hidden
users (cannot be attributed to a channel), and *edits* (a signal changed after posting is never
executed; it is audited for review).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from aegis.trading.signals import RawMessage


class IntakeReject(StrEnum):
    UNSUPPORTED_UPDATE = "unsupported_update"
    EDITED = "edited_message"
    UNKNOWN_INTAKE_CHAT = "unknown_intake_chat"
    UNTRUSTED_SENDER = "untrusted_sender"
    UNATTRIBUTABLE_FORWARD = "unattributable_forward"
    NO_TEXT = "no_text"


class MappedUpdate(BaseModel):
    update_id: int
    message: RawMessage | None = None
    reject: IntakeReject | None = None
    detail: str = ""
    delivered_via: int | None = None  # chat the bot actually received it in


class TelegramIntakeConfig(BaseModel):
    intake_chats: frozenset[int]  # channels/groups/DMs the bot listens in
    trusted_users: frozenset[int]  # user IDs allowed to post or forward into groups/DMs


def _ts(unix: int) -> datetime:
    return datetime.fromtimestamp(unix, UTC)


def map_update(update: dict[str, Any], cfg: TelegramIntakeConfig) -> MappedUpdate:
    uid = int(update.get("update_id", -1))

    def reject(reason: IntakeReject, detail: str = "", via: int | None = None) -> MappedUpdate:
        return MappedUpdate(update_id=uid, reject=reason, detail=detail, delivered_via=via)

    if "edited_message" in update or "edited_channel_post" in update:
        msg = update.get("edited_message") or update.get("edited_channel_post") or {}
        return reject(IntakeReject.EDITED, f"message {msg.get('message_id')}", msg.get("chat", {}).get("id"))

    is_channel_post = "channel_post" in update
    msg = update.get("channel_post") if is_channel_post else update.get("message")
    if not isinstance(msg, dict):
        return reject(IntakeReject.UNSUPPORTED_UPDATE)

    chat = msg.get("chat", {})
    chat_id = int(chat.get("id", 0))
    if chat_id not in cfg.intake_chats:
        return reject(IntakeReject.UNKNOWN_INTAKE_CHAT, str(chat_id), chat_id)

    text = msg.get("text") or msg.get("caption")
    if not text:
        return reject(IntakeReject.NO_TEXT, via=chat_id)

    if is_channel_post:
        source, message_id, posted = chat_id, int(msg["message_id"]), _ts(msg["date"])
    else:
        sender = (msg.get("from") or {}).get("id")
        if sender not in cfg.trusted_users:
            return reject(IntakeReject.UNTRUSTED_SENDER, str(sender), chat_id)
        origin = msg.get("forward_origin")
        if origin is None:
            source, message_id, posted = chat_id, int(msg["message_id"]), _ts(msg["date"])
        elif origin.get("type") == "channel":
            # Original channel + original message id: the same post forwarded twice, or also
            # received directly as channel_post, dedupes to one signal.
            source, message_id, posted = int(origin["chat"]["id"]), int(origin["message_id"]), _ts(origin["date"])
        elif origin.get("type") == "chat":
            source, message_id, posted = int(origin["sender_chat"]["id"]), int(msg["message_id"]), _ts(origin["date"])
        else:
            return reject(IntakeReject.UNATTRIBUTABLE_FORWARD, str(origin.get("type")), chat_id)

    raw = RawMessage(source_chat_id=source, message_id=message_id, text=text[:4096], posted_at=posted)
    return MappedUpdate(update_id=uid, message=raw, delivered_via=chat_id)
