from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.utils import timezone

import pytest
from sessions.header import build_session_header, ci_payload, issue_link, latest_run_payload, retry_for, watch_payload
from sessions.models import EventKind, MergeRequestOwnership, Run, RunStatus, Session, SessionOrigin
from sessions.transcript import annotate_transcript

from codebase.base import GitPlatform
from codebase.models import RepositoryCatalog
from tests.unit_tests.sessions.conftest import make_task_result
from tests.unit_tests.sessions.inbox.conftest import make_event


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
    assert latest_run_payload([], "") is None


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

    payload = latest_run_payload([*session.runs.order_by("created_at")], "again")

    assert payload == {
        "marker_id": f"run-status-{latest.id}",
        "label": label,
        "variant": variant,
        "finished_at": "",
        "retry": "again",
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("owner", "runnable", "offered"), [(True, True, True), (False, True, False), (True, False, False)]
)
def test_retry_for_needs_a_viewer_who_owns_the_session_and_can_run_on_it(member_user, owner, runnable, offered):
    session = _session(user=member_user if owner else None)
    runs = [_run(session, status=RunStatus.FAILED, prompt="fix it")]

    with patch("sessions.header.can_run", return_value=runnable) as can_run:
        retry = retry_for(session, runs, [], member_user)

    assert retry == ("fix it" if offered else "")
    can_run.assert_called_once_with(member_user, "group/project")


@pytest.mark.django_db
@pytest.mark.parametrize(("status", "can_act"), [(RunStatus.SUCCESSFUL, None), (RunStatus.FAILED, True)])
def test_retry_for_asks_for_run_access_only_when_it_has_to(member_user, status, can_act):
    session = _session(user=member_user)
    runs = [_run(session, status=status, prompt="fix it")]

    with patch("sessions.header.can_run") as can_run:
        retry_for(session, runs, [], member_user, can_act=can_act)

    can_run.assert_not_called()


@pytest.mark.django_db
def test_latest_run_payload_carries_when_the_run_finished():
    session = _session()
    finished = timezone.now()
    run = _run(session, finished_at=finished)

    assert latest_run_payload([run], "")["finished_at"] == finished.isoformat()


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


def _owned(session: Session, iid: int = 7, **fields) -> MergeRequestOwnership:
    defaults = {"repo_id": session.repo_id, "merge_request_iid": iid, "session": session, "source_branch": f"b{iid}"}
    ownership = MergeRequestOwnership.objects.create(**(defaults | fields))
    MergeRequestOwnership.objects.filter(pk=ownership.pk).update(updated_at=timezone.now() - timedelta(hours=1))
    ownership.refresh_from_db()
    return ownership


def _ci_event(session: Session, kind: str, *, iid: int = 7, run: Run | None = None, **fields):
    return make_event(session, kind, merge_request_iid=iid, run=run, created_at=timezone.now(), **fields)


@pytest.fixture
def policy():
    with patch("sessions.header.WatchPolicy") as policy:
        policy.return_value = SimpleNamespace(enabled=True, max_attempts=3)
        yield policy.return_value


@pytest.mark.django_db
class TestCiPayload:
    def test_a_session_that_owns_nothing_has_no_ci(self, policy):
        assert ci_payload(_session()) == []

    @pytest.mark.parametrize(
        ("kind", "status", "label", "tone"),
        [
            (EventKind.CI_FAILED, "failing", "Failing", "fail"),
            (EventKind.CI_PASSED, "passing", "Passing", "clear"),
            (EventKind.CI_STOPPED, "stopped", "Needs a person", "found"),
        ],
    )
    def test_the_latest_ci_result_is_the_status(self, policy, kind, status, label, tone):
        session = _session()
        _owned(session, attempts=1)
        _ci_event(session, kind, wake=False)

        assert ci_payload(session) == [
            {
                "merge_request_iid": 7,
                "status": status,
                "label": label,
                "tone": tone,
                "attempts": 1,
                "max_attempts": 3,
                "watch_enabled": True,
                "explanation_run_id": None,
            }
        ]

    def test_no_result_yet_is_running(self, policy):
        session = _session()
        _owned(session)

        assert ci_payload(session)[0]["status"] == "running"

    def test_a_publish_since_the_last_result_is_running(self, policy):
        session = _session()
        ownership = _owned(session)
        _ci_event(session, EventKind.CI_FAILED, wake=False)
        ownership.save()

        assert ci_payload(session)[0]["status"] == "running"

    def test_a_run_looking_at_a_failure_is_running(self, policy):
        session = _session()
        _owned(session)
        _ci_event(session, EventKind.CI_FAILED, run=_run(session, status=RunStatus.RUNNING))

        assert ci_payload(session)[0]["status"] == "running"

    def test_a_failure_links_the_last_run_that_looked_at_one(self, policy):
        session = _session()
        _owned(session, attempts=3)
        _ci_event(session, EventKind.CI_FAILED, run=_run(session))
        latest = _run(session)
        _ci_event(session, EventKind.CI_FAILED, run=latest)
        _ci_event(session, EventKind.CI_FAILED, wake=False)

        [item] = ci_payload(session)
        assert (item["status"], item["explanation_run_id"]) == ("failing", str(latest.pk))

    def test_the_watch_is_on_only_when_the_merge_request_and_the_repository_allow_it(self, policy):
        session = _session()
        _owned(session, watch_enabled=False)
        assert ci_payload(session)[0]["watch_enabled"] is False

        MergeRequestOwnership.objects.update(watch_enabled=True)
        policy.enabled = False
        assert ci_payload(session)[0]["watch_enabled"] is False

    def test_every_open_merge_request_is_listed_newest_claim_first(self, policy):
        session = _session()
        _owned(session, 7)
        _owned(session, 8).save()
        _owned(session, 9, closed_at=timezone.now())

        assert [item["merge_request_iid"] for item in ci_payload(session)] == [8, 7]


def test_watch_payload_is_the_first_merge_requests_ci_in_the_old_shape():
    ci = [
        {"merge_request_iid": 8, "status": "failing", "label": "Failing", "tone": "fail", "watch_enabled": True},
        {"merge_request_iid": 7, "status": "passing", "label": "Passing", "tone": "clear", "watch_enabled": False},
    ]

    assert watch_payload(ci) == {"state": "failing", "label": "Failing", "tone": "fail", "open": True}
    assert watch_payload([]) is None


@pytest.mark.django_db
def test_build_session_header_gathers_the_pages_context(policy):
    session = _session()
    _owned(session)
    _ci_event(session, EventKind.CI_PASSED, wake=False)
    runs = [_run(session), _run(session)]

    header = build_session_header(session, runs, retry="")

    assert header["latest_run"]["label"] == "Done"
    assert [item["status"] for item in header["ci"]] == ["passing"]
    assert header["watch"]["tone"] == "clear"
    assert header["issue"] is None
    assert header["run_count"] == 2


@pytest.mark.django_db
def test_latest_marker_id_names_the_red_publish_entry_of_a_failed_publish_run():
    session = _session()
    run = _run(session, status=RunStatus.FAILED, publish_status="failed", publish_error="mr_failed", message_id="h1")
    turns = [{"id": "h1", "role": "user", "segments": []}, {"id": "a1", "role": "assistant", "segments": []}]

    annotated = annotate_transcript(turns, [run])
    payload = latest_run_payload([run], "again")

    assert annotated[-1]["publish"][0]["id"] == payload["marker_id"]
