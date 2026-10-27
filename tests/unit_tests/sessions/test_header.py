from __future__ import annotations

import uuid
from unittest.mock import patch

from django.utils import timezone

import pytest
from sessions.header import build_session_header, issue_link, latest_run_payload, watch_payload
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState
from sessions.pipeline_watch.store import watch_thread_id

from codebase.base import GitPlatform
from codebase.models import RepositoryCatalog
from tests.unit_tests.sessions.conftest import make_task_result


def _session(**kwargs) -> Session:
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.CHAT, "repo_id": "group/project"}
    return Session.objects.create(**{**defaults, **kwargs})


def _run(session: Session, **kwargs) -> Run:
    defaults = {"trigger_type": session.origin, "repo_id": session.repo_id, "status": RunStatus.SUCCESSFUL}
    return Run.objects.create(session=session, **{**defaults, **kwargs})


def _issue_run(session: Session, iid: int | None) -> Run:
    task = make_task_result()
    task.args_kwargs = {"args": [], "kwargs": {"issue_iid": iid, "repo_id": session.repo_id}}
    task.save(update_fields=["args_kwargs"])
    return _run(session, trigger_type=SessionOrigin.ISSUE_WEBHOOK, task_result=task)


def _catalog(slug: str, html_url: str, provider: str = "gitlab") -> None:
    RepositoryCatalog.objects.create(provider=provider, slug=slug, html_url=html_url, synced_at=timezone.now())


@pytest.mark.django_db
def test_latest_run_payload_is_none_without_runs():
    assert latest_run_payload([], {}) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "variant", "label"),
    [
        (RunStatus.RUNNING, "running", "Running"),
        (RunStatus.FAILED, "failed", "Failed"),
        (RunStatus.SUCCESSFUL, "success", "Done"),
        (RunStatus.WAITING_INPUT, "waiting", "Needs input"),
        (RunStatus.QUEUED, "queued", "Queued"),
    ],
)
def test_latest_run_payload_describes_the_newest_run(status, variant, label):
    session = _session()
    _run(session, status=RunStatus.FAILED)
    _run(session, status=status)

    payload = latest_run_payload([*session.runs.order_by("created_at")], {})

    assert payload == {"label": label, "variant": variant, "finished_at": "", "retry_url": ""}


@pytest.mark.django_db
def test_latest_run_payload_carries_when_the_run_finished():
    session = _session()
    finished = timezone.now()
    run = _run(session, finished_at=finished)

    assert latest_run_payload([run], {})["finished_at"] == finished.isoformat()


@pytest.mark.django_db
def test_latest_run_payload_links_only_the_latest_runs_retry():
    session = _session(origin=SessionOrigin.UI_JOB)
    earlier, latest = _run(session), _run(session)

    assert latest_run_payload([earlier, latest], {str(earlier.id): "/retry-earlier"})["retry_url"] == ""
    assert latest_run_payload([earlier, latest], {str(latest.id): "/retry-latest"})["retry_url"] == "/retry-latest"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("platform", "html_url", "expected"),
    [
        (GitPlatform.GITLAB, "https://gitlab.example/group/project", "https://gitlab.example/group/project/-/issues/7"),
        (GitPlatform.GITHUB, "https://github.com/group/project/", "https://github.com/group/project/issues/7"),
    ],
)
def test_issue_link_points_at_the_issue_on_the_platform(platform, html_url, expected):
    session = _session(origin=SessionOrigin.ISSUE_WEBHOOK)
    run = _issue_run(session, 7)
    _catalog("group/project", html_url, provider=platform.value)

    with patch("sessions.header.codebase_settings.CLIENT", platform):
        assert issue_link(session, [run]) == {"iid": 7, "url": expected}


@pytest.mark.django_db
def test_issue_link_without_a_catalog_row_names_the_issue_unlinked():
    session = _session(origin=SessionOrigin.ISSUE_WEBHOOK)

    assert issue_link(session, [_issue_run(session, 7)]) == {"iid": 7, "url": ""}


@pytest.mark.django_db
def test_issue_link_is_none_when_no_issue_was_recorded():
    session = _session(origin=SessionOrigin.ISSUE_WEBHOOK)
    chat = _run(session, trigger_type=SessionOrigin.CHAT)

    assert issue_link(session, [chat]) is None
    assert issue_link(session, [_issue_run(session, None)]) is None


@pytest.mark.django_db
def test_issue_link_is_only_for_issue_webhook_sessions():
    session = _session(origin=SessionOrigin.MR_WEBHOOK)

    assert issue_link(session, [_issue_run(session, 7)]) is None


@pytest.mark.parametrize(
    ("state", "label", "tone", "is_open"),
    [
        (WatchState.WATCHING, "Waiting for CI", "attn", True),
        (WatchState.FIXING, "Fixing", "attn", True),
        (WatchState.GREEN, "Green", "clear", False),
        (WatchState.EXHAUSTED, "Attempts exhausted", "fail", False),
        (WatchState.UNCLEAR, "CI unreadable", "found", False),
    ],
)
def test_watch_payload_tones_the_ci_watch(state, label, tone, is_open):
    assert watch_payload(state) == {"state": state, "label": label, "tone": tone, "open": is_open}


@pytest.mark.parametrize("state", [WatchState.OFF, None])
def test_watch_payload_is_none_when_nothing_is_watched(state):
    assert watch_payload(state) is None


@pytest.mark.django_db
def test_build_session_header_gathers_the_pages_context():
    session = _session(watch_state=WatchState.GREEN)
    runs = [_run(session), _run(session)]

    header = build_session_header(session, runs, {})

    assert header["latest_run"]["label"] == "Done"
    assert header["watch"]["tone"] == "clear"
    assert header["issue"] is None
    assert header["run_count"] == 2


@pytest.mark.django_db
def test_merge_request_iid_prefers_the_checkpoints_then_the_sessions_then_the_newest_runs():
    from sessions.header import merge_request_iid

    session = _session()
    runs = [_run(session, merge_request_iid=3), _run(session, merge_request_iid=4), _run(session)]

    assert merge_request_iid(session, runs, {"id": 9}) == 9
    assert merge_request_iid(session, runs, None) == 4
    session.merge_request_iid = 5
    assert merge_request_iid(session, runs, None) == 5
    assert merge_request_iid(_session(), [], None) is None


@pytest.mark.django_db
def test_the_watch_is_read_off_the_merge_requests_thread():
    """A chat session that published the merge request shows the watch the MR's own thread holds."""
    from sessions.header import watch_state

    session = _session(watch_state=WatchState.OFF)
    mr_thread = _session(
        thread_id=watch_thread_id("group/project", 7), merge_request_iid=7, watch_state=WatchState.FIXING
    )

    assert watch_state(session, 7) == WatchState.FIXING
    assert watch_state(session, 8) is None
    assert watch_state(session, None) == WatchState.OFF
    assert watch_state(mr_thread, 7) == WatchState.FIXING


@pytest.mark.django_db(transaction=True)
async def test_awatch_state_reads_the_merge_requests_thread_but_not_the_session_itself():
    from asgiref.sync import sync_to_async
    from sessions.header import awatch_state

    session = await sync_to_async(_session)(watch_state=WatchState.OFF)
    mr_thread = await sync_to_async(_session)(
        thread_id=watch_thread_id("group/project", 7), merge_request_iid=7, watch_state=WatchState.WATCHING
    )
    await Session.objects.filter(pk=mr_thread.pk).aupdate(watch_state=WatchState.FIXING)

    assert await awatch_state(session, 7) == WatchState.FIXING
    assert await awatch_state(mr_thread, 7) == WatchState.WATCHING


@pytest.mark.django_db
def test_build_session_header_reads_the_watch_of_the_checkpoints_merge_request():
    session = _session()
    _session(thread_id=watch_thread_id("group/project", 7), merge_request_iid=7, watch_state=WatchState.GREEN)

    header = build_session_header(session, [], {}, merge_request={"id": 7})

    assert header["watch"]["state"] == WatchState.GREEN
