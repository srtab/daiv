from __future__ import annotations

import uuid
from unittest.mock import patch

from django.utils import timezone

import pytest
from sessions.header import build_session_header, issue_link, latest_run_payload
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState

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
    assert latest_run_payload([], user=None) is None


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
    latest = _run(session, status=status)

    payload = latest_run_payload([*session.runs.order_by("created_at")], user=None)

    assert payload == {"id": str(latest.id), "status": status, "label": label, "variant": variant, "retry_url": ""}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("trigger_type", "can_run", "retryable"),
    [(SessionOrigin.UI_JOB, True, True), (SessionOrigin.UI_JOB, False, False), (SessionOrigin.CHAT, True, False)],
)
def test_latest_run_payload_links_retry_only_for_a_retryable_run_the_viewer_can_run(trigger_type, can_run, retryable):
    session = _session(origin=trigger_type)
    run = _run(session, trigger_type=trigger_type, status=RunStatus.FAILED, prompt="fix it")

    with patch(
        "sessions.transcript.runnable_repo_ids", side_effect=lambda user, repo_ids: set(repo_ids) if can_run else set()
    ):
        payload = latest_run_payload([run], user=None)

    assert payload["retry_url"] == (f"/dashboard/runs/new/?from={run.id}" if retryable else "")


@pytest.mark.django_db
def test_latest_run_payload_ignores_an_earlier_retryable_run():
    session = _session(origin=SessionOrigin.UI_JOB)
    earlier = _run(session, trigger_type=SessionOrigin.UI_JOB, status=RunStatus.FAILED, prompt="fix it")
    latest = _run(session, trigger_type=SessionOrigin.CHAT, status=RunStatus.SUCCESSFUL)

    with patch("sessions.transcript.runnable_repo_ids", side_effect=lambda user, repo_ids: set(repo_ids)):
        payload = latest_run_payload([earlier, latest], user=None)

    assert payload["retry_url"] == ""


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


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("state", "tone"),
    [
        (WatchState.OFF, ""),
        (WatchState.WATCHING, "attn"),
        (WatchState.FIXING, "attn"),
        (WatchState.GREEN, "clear"),
        (WatchState.EXHAUSTED, "fail"),
        (WatchState.UNCLEAR, "found"),
    ],
)
def test_build_session_header_tones_the_ci_watch(state, tone):
    session = _session(watch_state=state)

    assert build_session_header(session, [], user=None) == {"latest_run": None, "issue": None, "watch_tone": tone}
