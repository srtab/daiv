"""Render the chat pages, so a suite only writes what it asserts about them.

Both the composer's own guards and the repo-wide surface guards need a rendered chat page,
and getting one means knowing which of ``sessions.views`` to patch — the hydration call
that would reach a checkpointer, and the merge-request lookup that would reach a platform.
That belongs in one place: a suite adding a page to its coverage should not have to learn
the patch list, and a new call to stub is then stubbed once rather than once per suite.

Public on purpose, unlike the module-private helpers a single suite keeps to itself — these
cross package boundaries (``tests/unit_tests/test_surface_stacking.py`` renders the same
pages the chat suite does).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from django.urls import reverse

from sessions.hydration import HydratedThread
from sessions.models import Session, SessionOrigin

_EMPTY_THREAD = HydratedThread([], False, None, None, None)


def create_session(user, **kwargs) -> Session:
    """A chat session owned by ``user``; ``kwargs`` override any of the defaults."""
    defaults = {
        "thread_id": str(uuid.uuid4()),
        "origin": SessionOrigin.CHAT,
        "repo_id": "group/project",
        "ref": "main",
        "user": user,
    }
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def render_new_chat(client) -> str:
    """The empty hero state: no thread yet, so the pickers are live."""
    with patch("sessions.views.ahydrate_thread", AsyncMock(return_value=_EMPTY_THREAD)):
        return client.get(reverse("session_new_chat")).content.decode()


def render_thread(client, session: Session) -> str:
    """An existing thread, with repo, ref, model and env pinned by its first turn."""
    with (
        patch("sessions.views.ahydrate_thread", AsyncMock(return_value=_EMPTY_THREAD)),
        patch("sessions.views.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        return client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id})).content.decode()
