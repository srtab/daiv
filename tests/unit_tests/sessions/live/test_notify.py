from __future__ import annotations

import uuid
from unittest.mock import call, patch

from django.db import DatabaseError, InterfaceError
from django.test import TestCase

import pytest
from asgiref.sync import sync_to_async
from sessions.live.notify import asession_changed, publish_session_changed, session_changed
from sessions.models import Session, SessionOrigin

PUBLISH = "core.ui_events.publisher.session_changed"


def _session(**fields) -> Session:
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.UI_JOB, "repo_id": "acme/api"}
    return Session.objects.create(**(defaults | fields))


@pytest.mark.django_db
class TestSessionChanged:
    def test_a_change_pokes_the_session_once_committed(self):
        session = _session()

        with patch(PUBLISH) as publish:
            with TestCase.captureOnCommitCallbacks(execute=False) as callbacks:
                session_changed(session.thread_id)

            publish.assert_not_called()

            for callback in callbacks:
                callback()

        publish.assert_called_once_with(session.thread_id)

    def test_a_delegated_session_also_pokes_its_parent(self):
        parent = _session(origin=SessionOrigin.API_JOB)
        child = _session(origin=SessionOrigin.DELEGATED_JOB, parent=parent)

        with patch(PUBLISH) as publish, TestCase.captureOnCommitCallbacks(execute=True):
            session_changed(child.thread_id)

        assert publish.call_args_list == [call(child.thread_id), call(parent.thread_id)]

    @pytest.mark.parametrize("thread_id", [None, ""])
    def test_no_thread_id_is_a_no_op(self, thread_id):
        with patch(PUBLISH) as publish, TestCase.captureOnCommitCallbacks(execute=False) as callbacks:
            session_changed(thread_id)

        assert callbacks == []
        publish.assert_not_called()

    @pytest.mark.parametrize("error", [DatabaseError, InterfaceError])
    def test_a_failed_parent_lookup_still_pokes_the_session(self, error):
        child = _session()

        with (
            patch(PUBLISH) as publish,
            patch.object(Session.objects, "filter", side_effect=error),
            TestCase.captureOnCommitCallbacks(execute=True),
        ):
            session_changed(child.thread_id)

        publish.assert_called_once_with(child.thread_id)

    def test_a_poke_that_did_not_go_out_skips_the_parent_lookup(self, django_assert_num_queries):
        """No bus, or a bus that just failed: the parent's poke would not go out either."""
        parent = _session(origin=SessionOrigin.API_JOB)
        child = _session(origin=SessionOrigin.DELEGATED_JOB, parent=parent)

        with patch(PUBLISH, return_value=False) as publish, django_assert_num_queries(0):
            publish_session_changed(child.thread_id)

        publish.assert_called_once_with(child.thread_id)


@pytest.mark.django_db(transaction=True)
class TestAsessionChanged:
    async def test_the_async_variant_publishes_at_once(self):
        parent = await sync_to_async(_session)(origin=SessionOrigin.API_JOB)
        child = await sync_to_async(_session)(origin=SessionOrigin.DELEGATED_JOB, parent=parent)

        with patch(PUBLISH) as publish:
            await asession_changed(child.thread_id)

        assert publish.call_args_list == [call(child.thread_id), call(parent.thread_id)]

    async def test_no_thread_id_is_a_no_op(self):
        with patch(PUBLISH) as publish:
            await asession_changed(None)

        publish.assert_not_called()
