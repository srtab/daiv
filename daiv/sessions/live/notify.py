"""The one way to tell a session's page that something it shows has changed.

A poke bumps the session's revision and wakes the viewers of its page, which then re-read it; it carries no
state of its own. A delegated session also pokes its parent, whose page lists it. Callers pick the variant by
context: ``session_changed`` from sync code (it waits for the surrounding transaction to commit),
``asession_changed`` from async code running in autocommit.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial

from django.db import Error, close_old_connections, transaction

from core import ui_events

logger = logging.getLogger("daiv.sessions")


def publish_session_changed(thread_id: str) -> None:
    """SYNC ONLY: poke ``thread_id``'s page, then its parent's. Never raises."""
    from sessions.models import Session

    ui_events.publisher.session_changed(thread_id)
    try:
        parent_id = Session.objects.filter(pk=thread_id).values_list("parent_id", flat=True).first()
    except Error:
        logger.warning(
            "live: parent lookup failed for thread_id=%s; its parent was not poked", thread_id, exc_info=True
        )
        return
    if parent_id:
        ui_events.publisher.session_changed(parent_id)


def session_changed(thread_id: str | None) -> None:
    """SYNC ONLY: poke ``thread_id``'s page once the current transaction commits."""
    if not thread_id:
        return
    transaction.on_commit(partial(publish_session_changed, thread_id))


async def asession_changed(thread_id: str | None) -> None:
    """Poke ``thread_id``'s page now; for async callers, which run in autocommit."""
    if not thread_id:
        return
    await asyncio.to_thread(_publish_in_thread, thread_id)


async def aownership_changed(ownership_id: int) -> None:
    """Poke the page of the session that owns merge request ownership ``ownership_id``."""
    from sessions.models import MergeRequestOwnership

    thread_id = (
        await MergeRequestOwnership.objects.filter(pk=ownership_id).values_list("session_id", flat=True).afirst()
    )
    await asession_changed(thread_id)


def _publish_in_thread(thread_id: str) -> None:
    try:
        publish_session_changed(thread_id)
    finally:
        # Runs in an executor thread no request lifecycle closes, so its pooled connection would be held forever.
        close_old_connections()
