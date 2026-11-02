import uuid

import pytest
from sessions.inbox_display import bubble, reason_text, run_bubble
from sessions.models import EventKind, Run, RunStatus, Session, SessionOrigin

from tests.unit_tests.sessions.inbox.conftest import chat_payload, ci_payload, make_event, mention_payload


@pytest.fixture
def session(db) -> Session:
    return Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/repo", ref="feat/x"
    )


@pytest.mark.parametrize(
    ("kind", "reason", "text"),
    [
        ("ci_failed", "not_daiv_commit", "not fixing: not DAIV's commit"),
        ("ci_failed", "already_examined", "not fixing: DAIV already looked at this commit"),
        ("ci_failed", "budget_spent", "not fixing: out of attempts"),
        ("ci_failed", "watch_off", "not fixing: CI fixing is off"),
        ("ci_failed", "run_limit", "not fixing: paused after 3 runs in a row"),
        ("delegation_finished", "run_limit", "not acting: paused after 3 runs in a row"),
    ],
)
def test_reason_text_per_quiet_reason(kind, reason, text):
    assert reason_text(kind, reason) == text


@pytest.mark.parametrize(
    ("attempts", "jobs", "text"),
    [
        (3, ["tests", "lint"], "not fixing: stopped after 3 attempts, failing: tests, lint"),
        (1, [], "not fixing: stopped after 1 attempt"),
    ],
)
def test_a_give_up_says_how_many_attempts_and_which_jobs_fail(attempts, jobs, text):
    payload = ci_payload(jobs=jobs) | {"attempts": attempts}

    assert reason_text("ci_failed", "budget_spent", payload) == text


def test_a_give_up_recorded_without_its_attempts_is_out_of_attempts():
    assert reason_text("ci_failed", "budget_spent", ci_payload()) == "not fixing: out of attempts"


def test_reason_text_is_empty_without_a_reason():
    assert reason_text("ci_failed", "") == ""


def test_a_ci_bubble_is_received_with_its_failed_jobs(session):
    event = make_event(
        session, EventKind.CI_FAILED, merge_request_iid=101, payload=ci_payload(8812, jobs=["test-unit (3/4)"])
    )

    result = bubble(event)

    assert result["received"] is True
    assert result["failed"] is True
    assert result["text"] == "Pipeline 8812 failed: test-unit (3/4)"
    assert result["source_iid"] == 101
    assert result["web_url"] == "https://gitlab.example/pipelines/8812"


def test_a_ci_bubble_with_no_failed_jobs_names_only_the_pipeline(session):
    event = make_event(session, EventKind.CI_FAILED, payload=ci_payload(8812, jobs=()))

    assert bubble(event)["text"] == "Pipeline 8812 failed"


@pytest.mark.parametrize(
    ("jobs", "text"), [(["ruff"], "Lint run 8812 failed: ruff"), ([], "Lint run 8812 failed")], ids=["jobs", "none"]
)
def test_a_github_ci_bubble_names_the_workflow_that_failed(session, jobs, text):
    payload = ci_payload(8812, jobs=jobs) | {"workflow": "Lint"}

    assert bubble(make_event(session, EventKind.CI_FAILED, payload=payload))["text"] == text


def test_a_finished_delegation_bubble_says_how_many_jobs_succeeded(session):
    payload = {"batch_id": "b", "summary": "…", "succeeded": 3, "total": 3}
    result = bubble(make_event(session, EventKind.DELEGATION_FINISHED, payload=payload))

    assert (result["received"], result["failed"], result["text"]) == (True, False, "3 of 3 jobs succeeded")


def test_a_finished_delegation_with_a_failed_job_is_failed(session):
    payload = {"batch_id": "b", "summary": "…", "succeeded": 0, "total": 1}
    result = bubble(make_event(session, EventKind.DELEGATION_FINISHED, payload=payload))

    assert (result["failed"], result["text"]) == (True, "0 of 1 job succeeded")


def test_a_finished_delegation_with_no_outcome_leaves_the_text_to_its_source_line(session):
    result = bubble(make_event(session, EventKind.DELEGATION_FINISHED))

    assert (result["failed"], result["text"]) == (False, "")


def test_a_mention_bubble_names_its_author_and_merge_request(session):
    event = make_event(
        session,
        EventKind.MENTION,
        merge_request_iid=104,
        author_username="carol",
        payload=mention_payload("@daiv rebase this on main please"),
    )

    result = bubble(event)

    assert result["text"] == "carol mentioned you on !104: @daiv rebase this on main please"
    assert result["received"] is False
    assert result["id"] == f"inbox-{event.pk}"
    assert result["event_id"] == str(event.pk)


def test_a_long_chat_message_is_not_cut(session):
    text = "word " * 100

    assert bubble(make_event(session, EventKind.CHAT_MESSAGE, payload=chat_payload(text)))["text"] == text


def test_a_follow_up_job_bubble_carries_its_prompt_and_trigger(session):
    result = bubble(make_event(session, EventKind.FOLLOW_UP_JOB))

    assert (result["text"], result["trigger_type"]) == ("Bump the version", SessionOrigin.API_JOB)


def test_a_queued_job_run_bubble_carries_its_prompt_and_trigger(session):
    run = Run.objects.create(
        session=session,
        trigger_type=SessionOrigin.MCP_JOB,
        repo_id=session.repo_id,
        status=RunStatus.QUEUED,
        prompt="Upgrade Django",
    )

    result = run_bubble(run)

    assert result == {
        "id": f"run-{run.id}",
        "event_id": "",
        "kind": "job",
        "received": False,
        "failed": False,
        "text": "Upgrade Django",
        "source_iid": None,
        "trigger_type": SessionOrigin.MCP_JOB,
        "web_url": "",
        "created_at": run.created_at.isoformat(),
    }
