"""Lenient Bot API update models.

Every field is optional and every model ignores extras, so an update shape DAIV does not
model still parses. That is deliberate: the webhook route takes the raw body rather than
declaring a pydantic payload, because django-ninja answers a schema mismatch with 422 — a
non-2xx that Telegram retries and eventually punishes by disabling the webhook.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger("daiv.notifications")

# ``/name[@botusername] [argument]``. The ``@botusername`` suffix is how Telegram addresses a
# command in a group; harmless in a private chat, and stripping it here keeps the registry keys clean.
_COMMAND_RE = re.compile(r"\A/(?P<name>[A-Za-z0-9_]{1,32})(?:@[A-Za-z0-9_]+)?(?:\s+(?P<argument>.*))?\Z", re.DOTALL)


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TGChat(_Lenient):
    id: int | None = None
    type: str | None = None
    username: str | None = None
    first_name: str | None = None


class TGMessage(_Lenient):
    message_id: int | None = None
    chat: TGChat | None = None
    text: str | None = None


class TGChatMember(_Lenient):
    status: str | None = None


class TGChatMemberUpdated(_Lenient):
    chat: TGChat | None = None
    new_chat_member: TGChatMember | None = None


class TGUpdate(_Lenient):
    update_id: int | None = None
    message: TGMessage | None = None
    my_chat_member: TGChatMemberUpdated | None = None


def parse_update(raw: bytes) -> TGUpdate | None:
    """Parse a webhook body, or ``None`` when it is not an object we can read at all."""
    try:
        return TGUpdate.model_validate_json(raw)
    except ValidationError:
        logger.warning("Telegram: unreadable update body (%d bytes); answering 204", len(raw))
        return None


def parse_command(text: str | None) -> tuple[str, str] | None:
    """``(name, argument)`` for a bot command, or ``None`` for ordinary text."""
    if not text:
        return None
    match = _COMMAND_RE.match(text.strip())
    if match is None:
        return None
    return match.group("name").lower(), (match.group("argument") or "").strip()


def is_private_chat(chat: TGChat | None) -> bool:
    """True only for one-to-one chats.

    Group ids are negative, and binding one would make a whole room the recipient of one
    account's notifications.
    """
    return chat is not None and chat.type == "private"


def display_handle(chat: TGChat | None) -> str:
    """The human-readable handle to store alongside the numeric chat id."""
    if chat is None:
        return ""
    return chat.username or chat.first_name or ""
