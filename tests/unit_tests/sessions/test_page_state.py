from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, call, patch

from django.utils import timezone

import pytest
from gitlab.exceptions import GitlabError
from sessions.hydration import HydratedThread
from sessions.models import Run, RunStatus, Session, SessionOrigin, SessionRepository
from sessions.page_state import aload_session_state, turns_payload

MERGE_REQUEST = {
    "id": 5,
    "url": "https://example.com/mr/5",
    "title": "Fix",
    "draft": False,
    "source_branch": "daiv/fix",
    "target_branch": "main",
}


async def _session(user, *, in_flight: bool) -> Session:
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="group/project", ref="feature", user=user
    )
    status = RunStatus.RUNNING if in_flight else RunStatus.SUCCESSFUL
    run = await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.UI_JOB, repo_id=session.repo_id, status=status
    )
    if in_flight:
        session.active_run_id = str(run.pk)
        await session.asave(update_fields=["active_run_id"])
    return session


def _hydration(merge_request=None):
    return AsyncMock(return_value=HydratedThread([], False, merge_request, None, None))


@pytest.mark.django_db(transaction=True)
async def test_the_revision_is_read_before_the_state_is_built(member_user):
    session = await _session(member_user, in_flight=False)
    order = Mock()
    order.attach_mock(AsyncMock(return_value=7), "revision")
    order.attach_mock(_hydration(), "hydrate")

    with (
        patch("sessions.page_state.aread_session_revision", order.revision),
        patch("sessions.page_state.ahydrate_thread", order.hydrate),
        patch("sessions.page_state.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        state = await aload_session_state(session, member_user, always_look_up_merge_request=False)

    assert order.mock_calls == [call.revision(session.thread_id), call.hydrate(session.thread_id)]
    assert state.revision == 7


@pytest.mark.django_db(transaction=True)
async def test_a_routine_fetch_does_not_ask_the_platform_while_in_flight(member_user):
    session = await _session(member_user, in_flight=True)
    lookup = AsyncMock(return_value=MERGE_REQUEST)

    with (
        patch("sessions.page_state.ahydrate_thread", _hydration()),
        patch("sessions.page_state.aget_existing_mr_payload", lookup),
    ):
        state = await aload_session_state(session, member_user, always_look_up_merge_request=False)

    lookup.assert_not_awaited()
    assert state.activity.in_flight is True
    assert (state.merge_request, state.thread_ref) == (None, "feature")


@pytest.mark.django_db(transaction=True)
async def test_a_settled_fetch_does_look_up_the_merge_request(member_user):
    session = await _session(member_user, in_flight=False)
    lookup = AsyncMock(return_value=MERGE_REQUEST)

    with (
        patch("sessions.page_state.ahydrate_thread", _hydration()),
        patch("sessions.page_state.aget_existing_mr_payload", lookup),
    ):
        state = await aload_session_state(session, member_user, always_look_up_merge_request=False)

    lookup.assert_awaited_once_with("group/project", "feature")
    assert (state.merge_request, state.thread_ref) == (MERGE_REQUEST, "daiv/fix")


@pytest.mark.django_db(transaction=True)
async def test_the_render_always_looks_it_up(member_user):
    session = await _session(member_user, in_flight=True)
    lookup = AsyncMock(return_value=MERGE_REQUEST)

    with (
        patch("sessions.page_state.ahydrate_thread", _hydration()),
        patch("sessions.page_state.aget_existing_mr_payload", lookup),
    ):
        state = await aload_session_state(session, member_user, always_look_up_merge_request=True)

    lookup.assert_awaited_once_with("group/project", "feature")
    assert state.merge_request == MERGE_REQUEST


@pytest.mark.django_db(transaction=True)
async def test_settled_fetches_within_a_minute_ask_the_platform_once(member_user):
    session = await _session(member_user, in_flight=False)
    lookup = AsyncMock(return_value=None)

    with (
        patch("sessions.page_state.ahydrate_thread", _hydration()),
        patch("sessions.page_state.aget_existing_mr_payload", lookup),
    ):
        first = await aload_session_state(session, member_user, always_look_up_merge_request=False)
        second = await aload_session_state(session, member_user, always_look_up_merge_request=False)

    lookup.assert_awaited_once_with("group/project", "feature")
    assert "merge_request" in turns_payload(first, session)
    assert turns_payload(second, session)["merge_request"] is None


@pytest.mark.django_db(transaction=True)
async def test_the_render_refreshes_the_merge_request_a_fetch_reuses(member_user):
    session = await _session(member_user, in_flight=False)
    lookup = AsyncMock(side_effect=[None, MERGE_REQUEST])

    with (
        patch("sessions.page_state.ahydrate_thread", _hydration()),
        patch("sessions.page_state.aget_existing_mr_payload", lookup),
    ):
        await aload_session_state(session, member_user, always_look_up_merge_request=False)
        rendered = await aload_session_state(session, member_user, always_look_up_merge_request=True)
        fetched = await aload_session_state(session, member_user, always_look_up_merge_request=False)

    assert lookup.await_count == 2
    assert rendered.merge_request == fetched.merge_request == MERGE_REQUEST


@pytest.mark.django_db(transaction=True)
async def test_a_failed_lookup_is_not_cached_and_leaves_the_merge_request_out(member_user):
    session = await _session(member_user, in_flight=False)
    lookup = AsyncMock(side_effect=[GitlabError("api 500"), MERGE_REQUEST])

    with (
        patch("sessions.page_state.ahydrate_thread", _hydration()),
        patch("sessions.page_state.aget_existing_mr_payload", lookup),
    ):
        failed = await aload_session_state(session, member_user, always_look_up_merge_request=False)
        retried = await aload_session_state(session, member_user, always_look_up_merge_request=False)

    assert "merge_request" not in turns_payload(failed, session)
    assert failed.thread_ref == "feature"
    assert turns_payload(retried, session)["merge_request"] == MERGE_REQUEST
    assert lookup.await_count == 2


@pytest.mark.django_db(transaction=True)
async def test_the_checkpointed_merge_request_needs_no_lookup(member_user):
    session = await _session(member_user, in_flight=False)
    lookup = AsyncMock(return_value=None)

    with (
        patch("sessions.page_state.ahydrate_thread", _hydration(MERGE_REQUEST)),
        patch("sessions.page_state.aget_existing_mr_payload", lookup),
    ):
        state = await aload_session_state(session, member_user, always_look_up_merge_request=True)

    lookup.assert_not_awaited()
    assert state.merge_request == MERGE_REQUEST


@pytest.mark.django_db(transaction=True)
async def test_the_retry_follows_the_run_access_the_state_reports(member_user):
    session = await _session(member_user, in_flight=False)
    await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.UI_JOB,
        repo_id=session.repo_id,
        status=RunStatus.FAILED,
        prompt="fix it",
    )

    with (
        patch("sessions.page_state.ahydrate_thread", _hydration()),
        patch("sessions.page_state.aget_existing_mr_payload", AsyncMock(return_value=None)),
        patch("sessions.page_state.can_run", Mock(return_value=False)),
        patch("sessions.header.can_run", Mock(return_value=True)),
    ):
        state = await aload_session_state(session, member_user, always_look_up_merge_request=False)

    assert state.can_act is False
    assert state.header["latest_run"]["retry"] == ""


@pytest.mark.django_db(transaction=True)
async def test_a_row_change_that_lands_with_the_revision_is_in_the_state(member_user):
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.UI_JOB,
        repo_id="group/project",
        ref="feature",
        user=member_user,
        last_active_at=timezone.now() - timedelta(days=1),
    )
    run = await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.UI_JOB, repo_id=session.repo_id, status=RunStatus.RUNNING
    )

    async def claim_then_read(thread_id):
        await Session.objects.filter(pk=thread_id).aupdate(
            active_run_id=str(run.pk), last_active_at=timezone.now(), title="Renamed", ref="daiv/fix"
        )
        return 8

    with (
        patch("sessions.page_state.aread_session_revision", AsyncMock(side_effect=claim_then_read)),
        patch("sessions.page_state.ahydrate_thread", _hydration()),
        patch("sessions.page_state.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        state = await aload_session_state(session, member_user, always_look_up_merge_request=False)

    assert state.live_run is not None
    assert state.live_run.id == str(run.pk)
    payload = turns_payload(state, session)
    assert (payload["active"], payload["thread_ref"], payload["title"]) == (True, "daiv/fix", "Renamed")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("in_flight", "banner"), [(False, True), (True, False)])
async def test_the_payload_says_whether_a_render_would_show_the_expired_banner(member_user, in_flight, banner):
    """A run going on an expired session leaves the banner while the checkpoint is still missing."""
    session = await _session(member_user, in_flight=in_flight)
    expired = AsyncMock(return_value=HydratedThread([], True, None, None, None))

    with (
        patch("sessions.page_state.ahydrate_thread", expired),
        patch("sessions.page_state.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        state = await aload_session_state(session, member_user, always_look_up_merge_request=False)

    payload = turns_payload(state, session)
    assert (payload["expired"], payload["expired_banner"]) == (True, banner)


@pytest.mark.django_db(transaction=True)
async def test_the_payload_carries_what_a_turn_did_to_each_repository(member_user):
    session = await _session(member_user, in_flight=False)
    await SessionRepository.objects.acreate(session=session, position=1, repo_id="group/lib", ref="feature")

    async def fetch():
        with (
            patch("sessions.page_state.ahydrate_thread", _hydration()),
            patch("sessions.page_state.aget_existing_mr_payload", AsyncMock(return_value=MERGE_REQUEST)),
        ):
            state = await aload_session_state(session, member_user, always_look_up_merge_request=False)
        return turns_payload(state, session)

    before = await fetch()
    await SessionRepository.objects.filter(session=session).aupdate(
        ref="daiv/lib", merge_request_iid=3, merge_request_web_url="https://example.com/lib/mr/3"
    )
    await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.UI_JOB,
        repo_id=session.repo_id,
        status=RunStatus.SUCCESSFUL,
        publish_errors=[{"repo_id": "group/other", "error": "push rejected"}],
    )
    after = await fetch()

    details = {"mr_title": "", "mr_draft": False, "diff_stats": None, "ran": True}
    main = {"repo_id": "group/project", "ref": "feature", "mr_url": "", "mr_iid": None, **details}
    lib = {"repo_id": "group/lib", "ref": "feature", "mr_url": "", "mr_iid": None, **details}
    published = {
        "repo_id": "group/lib",
        "ref": "daiv/lib",
        "mr_url": "https://example.com/lib/mr/3",
        "mr_iid": 3,
        **details,
    }
    assert before["repositories"] == {"rows": [main, lib], "publish_errors": []}
    assert after["repositories"] == {
        "rows": [main, published],
        "publish_errors": [{"repo_id": "group/other", "error": "push rejected"}],
    }
