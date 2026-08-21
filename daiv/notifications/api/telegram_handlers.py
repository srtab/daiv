"""The concrete `/start`, `/stop` and `my_chat_member` handlers.

They live here rather than in ``notifications/telegram/`` because they read and write
``UserChannelBinding``: the transport's dependency arrow points one way, and these register
into its registry from this side.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

from accounts.models import User
from notifications.telegram.commands import BaseCommand, register_command
from notifications.telegram.schemas import display_handle
from notifications.telegram.tokens import peek_user_pk, verify_token
from notifications.telegram_bindings import bind_chat, binding_state_for_pk, unbind_chat

if TYPE_CHECKING:
    from notifications.telegram.schemas import TGChat, TGChatMemberUpdated

logger = logging.getLogger("daiv.notifications")

# ``left`` covers a user deleting the chat; ``kicked`` is a block. Either way the chat can no
# longer receive, so keeping the binding would only buy a permanent failure per notification.
_BLOCKED_STATUSES = frozenset({"kicked", "left"})

MSG_LINK_EXPIRED = _("That link has expired. Start again from your DAIV notification channels page.")
MSG_BARE_START = _("Open your DAIV notification channels page and select Connect to link this chat.")
MSG_PRIVATE_ONLY = _("Message me directly — DAIV notifications are per-account and cannot go to a group.")
MSG_CONNECTED = _("Connected. DAIV will send your notifications here.")
MSG_DISCONNECTED = _("Disconnected. DAIV will stop sending notifications here.")
MSG_NOT_CONNECTED = _("This chat is not connected to a DAIV account.")


@register_command
class StartCommand(BaseCommand):
    name = "start"

    def handle(self, chat: TGChat, argument: str) -> str | None:
        if not argument:
            return str(MSG_BARE_START)
        # Two-step by design: the pk is only a claim until verify_token authenticates it
        # against the binding state the token was minted over.
        user_pk = peek_user_pk(argument)
        if user_pk is None:
            return str(MSG_LINK_EXPIRED)
        address, verified_at = binding_state_for_pk(user_pk)
        if verify_token(argument, address=address, verified_at=verified_at) != user_pk:
            return str(MSG_LINK_EXPIRED)
        user = User.objects.filter(pk=user_pk, is_active=True).first()
        if user is None:
            return str(MSG_LINK_EXPIRED)
        bind_chat(user, chat_id=str(chat.id), handle=display_handle(chat))
        logger.info("Telegram: bound chat %s to user pk=%s", chat.id, user_pk)
        return str(MSG_CONNECTED)


@register_command
class StopCommand(BaseCommand):
    name = "stop"

    def handle(self, chat: TGChat, argument: str) -> str | None:
        removed = unbind_chat(str(chat.id))
        return str(MSG_DISCONNECTED if removed else MSG_NOT_CONNECTED)


def handle_my_chat_member(update: TGChatMemberUpdated) -> None:
    """Unbind on a block, immediately rather than at the next delivery's 403."""
    chat, member = update.chat, update.new_chat_member
    if chat is None or chat.id is None or member is None or member.status not in _BLOCKED_STATUSES:
        return
    if removed := unbind_chat(str(chat.id)):
        logger.info("Telegram: chat %s is %s; removed %d binding(s)", chat.id, member.status, removed)
