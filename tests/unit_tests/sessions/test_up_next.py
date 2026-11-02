import uuid

from django.utils import timezone

import pytest
from sessions.models import EventKind, EventState, Run, RunStatus, Session, SessionOrigin
from sessions.up_next import up_next_payload

from accounts.models import Role, User
from tests.unit_tests.sessions.inbox.conftest import chat_payload, ci_payload, make_event, mention_payload

pytestmark = pytest.mark.django_db


def _user(username: str, name: str = "") -> User:
    return User.objects.create_user(
        username=username,
        email=f"{username}@test.com",
        name=name,
        password="x",  # noqa: S106
        role=Role.MEMBER,
    )


@pytest.fixture
def owner() -> User:
    return _user("owner", "Olga Owner")


@pytest.fixture
def session(owner) -> Session:
    return Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/repo", ref="feat/x", user=owner
    )


def _run(session: Session, *, status=RunStatus.QUEUED, trigger=SessionOrigin.CHAT, user=None, **fields) -> Run:
    return Run.objects.create(
        session=session,
        trigger_type=trigger,
        repo_id=session.repo_id,
        ref=session.ref,
        status=status,
        user=user,
        **fields,
    )


def _busy(session: Session) -> Run:
    """A chat run holding ``session``'s slot."""
    holder = _run(session, status=RunStatus.RUNNING, user=session.user)
    Session.objects.filter(pk=session.pk).update(active_run_id=str(holder.pk), last_active_at=timezone.now())
    session.refresh_from_db()
    return holder


def _payload(session: Session, viewer: User) -> dict | None:
    return up_next_payload(session, list(session.runs.order_by("created_at")), viewer)


def test_nothing_pending_is_none(session, owner):
    assert _payload(session, owner) is None


def test_two_requesters_get_a_group_each_in_the_order_they_asked(session, owner):
    reviewer = _user("rev", "Bruno Reviewer")
    _busy(session)
    mine = _run(session, user=owner)
    theirs = _run(session, user=reviewer, trigger=SessionOrigin.MR_WEBHOOK)
    chat = make_event(session, EventKind.CHAT_MESSAGE, run=mine, author=owner)
    mention = make_event(
        session, EventKind.MENTION, run=theirs, author=reviewer, merge_request_iid=104, payload=mention_payload()
    )
    job = make_event(session, EventKind.FOLLOW_UP_JOB, run=mine, author=owner)
    push = make_event(session, EventKind.BRANCH_PUSHED, ref=session.ref, payload=_push())

    payload = _payload(session, owner)

    first, second = payload["groups"]
    assert (first["run_id"], first["is_viewer"], first["starting"]) == (str(mine.pk), True, False)
    assert [bubble["id"] for bubble in first["bubbles"]] == [f"inbox-{chat.pk}", f"inbox-{job.pk}"]
    assert [line["id"] for line in first["told"]] == [f"inbox-{push.pk}"]
    assert (second["who"], second["is_viewer"]) == ("Bruno Reviewer", False)
    assert [bubble["id"] for bubble in second["bubbles"]] == [f"inbox-{mention.pk}"]
    assert payload["told"] == []


def test_a_builtin_command_is_a_group_of_its_own_where_the_drain_runs_it(session, owner):
    reviewer = _user("rev", "Bruno Reviewer")
    _busy(session)
    mine = _run(session, user=owner)
    theirs = _run(session, user=reviewer, trigger=SessionOrigin.MR_WEBHOOK)
    before = make_event(session, EventKind.CHAT_MESSAGE, run=mine, author=owner, payload=chat_payload("Fix it"))
    command = make_event(session, EventKind.CHAT_MESSAGE, run=mine, author=owner, payload=chat_payload("/help"))
    mention = make_event(
        session, EventKind.MENTION, run=theirs, author=reviewer, merge_request_iid=104, payload=mention_payload()
    )
    after = make_event(session, EventKind.CHAT_MESSAGE, run=mine, author=owner, payload=chat_payload("Add a test"))
    push = make_event(session, EventKind.BRANCH_PUSHED, ref=session.ref, payload=_push())

    groups = _payload(session, owner)["groups"]

    assert [[bubble["id"] for bubble in group["bubbles"]] for group in groups] == [
        [f"inbox-{before.pk}"],
        [f"inbox-{command.pk}"],
        [f"inbox-{mention.pk}"],
        [f"inbox-{after.pk}"],
    ]
    assert [group["run_id"] for group in groups] == ["", "", str(theirs.pk), str(mine.pk)]
    assert [[line["id"] for line in group["told"]] for group in groups] == [[f"inbox-{push.pk}"], [], [], []]


def test_remembered_events_wait_for_the_run_after_a_leading_command(session, owner):
    _busy(session)
    mine = _run(session, user=owner)
    make_event(session, EventKind.CHAT_MESSAGE, run=mine, author=owner, payload=chat_payload("/agents"))
    make_event(session, EventKind.CHAT_MESSAGE, run=mine, author=owner, payload=chat_payload("Add a test"))
    push = make_event(session, EventKind.BRANCH_PUSHED, ref=session.ref, payload=_push())

    command, rest = _payload(session, owner)["groups"]

    assert (command["told"], [line["id"] for line in rest["told"]]) == ([], [f"inbox-{push.pk}"])


def test_a_session_at_the_run_limit_lists_a_finished_delegation_as_told(session, owner):
    for _ in range(3):
        _run(session, status=RunStatus.SUCCESSFUL, trigger=SessionOrigin.PIPELINE_WEBHOOK)
    next_run = _run(session, trigger=SessionOrigin.DELEGATION_RESUME, user=owner)
    event = make_event(session, EventKind.DELEGATION_FINISHED, run=next_run)

    payload = _payload(session, owner)

    assert payload["groups"] == []
    (block,) = payload["told"]
    assert block["branch"] == ""
    assert [(line["id"], line["reason_text"]) for line in block["lines"]] == [
        (f"inbox-{event.pk}", "not acting: paused after 3 runs in a row")
    ]


def test_a_remembered_event_on_another_branch_gets_its_own_block(session, owner):
    event = make_event(session, EventKind.CI_PASSED, ref="feat/other", merge_request_iid=7)

    payload = _payload(session, owner)

    assert payload["groups"] == []
    assert [(block["branch"], [line["id"] for line in block["lines"]]) for block in payload["told"]] == [
        ("feat/other", [f"inbox-{event.pk}"])
    ]


def test_a_promoted_run_not_yet_started_is_starting(session, owner):
    promoted = _run(session, status=RunStatus.READY, user=owner)
    Session.objects.filter(pk=session.pk).update(active_run_id=str(promoted.pk), last_active_at=timezone.now())
    session.refresh_from_db()
    chat = make_event(session, EventKind.CHAT_MESSAGE, run=promoted, state=EventState.TAKEN, author=owner)
    comment = make_event(session, EventKind.COMMENT, run=promoted, state=EventState.TAKEN)

    (group,) = _payload(session, owner)["groups"]

    assert (group["run_id"], group["starting"], group["who"]) == (str(promoted.pk), True, "Olga Owner")
    assert [bubble["id"] for bubble in group["bubbles"]] == [f"inbox-{chat.pk}"]
    assert [line["id"] for line in group["told"]] == [f"inbox-{comment.pk}"]


def test_inbox_runs_come_before_a_queued_job_run(session, owner):
    _busy(session)
    job = _run(session, trigger=SessionOrigin.API_JOB, prompt="Upgrade Django")
    next_run = _run(session, user=owner)
    make_event(session, EventKind.CHAT_MESSAGE, run=next_run, author=owner)

    first, second = _payload(session, owner)["groups"]

    assert first["run_id"] == str(next_run.pk)
    assert [bubble["id"] for bubble in second["bubbles"]] == [f"run-{job.pk}"]


def test_a_ci_failure_the_watch_no_longer_covers_is_told_not_run(session, owner):
    from sessions.models import MergeRequestOwnership

    MergeRequestOwnership.objects.create(repo_id=session.repo_id, merge_request_iid=101, watch_enabled=False)
    next_run = _run(session, trigger=SessionOrigin.PIPELINE_WEBHOOK, user=owner)
    make_event(session, EventKind.CI_FAILED, run=next_run, merge_request_iid=101, payload=ci_payload(8812))

    payload = _payload(session, owner)

    assert payload["groups"] == []
    assert payload["told"][0]["lines"][0]["reason_text"] == "not fixing: CI fixing is off"


def test_an_older_ci_failure_a_newer_result_replaces_is_left_out(session, owner):
    next_run = _run(session, trigger=SessionOrigin.PIPELINE_WEBHOOK, user=owner)
    make_event(session, EventKind.CI_FAILED, run=next_run, subject="ci:101", merge_request_iid=101)
    passed = make_event(session, EventKind.CI_PASSED, subject="ci:101", merge_request_iid=101)

    payload = _payload(session, owner)

    assert payload["groups"] == []
    assert [line["id"] for line in payload["told"][0]["lines"]] == [f"inbox-{passed.pk}"]


def test_another_viewer_is_not_you(session, owner):
    next_run = _run(session, user=owner)
    make_event(session, EventKind.CHAT_MESSAGE, run=next_run, author=owner)

    (group,) = _payload(session, _user("reader"))["groups"]

    assert (group["who"], group["is_viewer"]) == ("Olga Owner", False)


def _push() -> dict:
    return {"before": "a" * 40, "after": "b" * 40, "commit_count": 1, "pusher_username": "sfr"}


def test_a_run_the_conversation_shows_as_waiting_is_left_out(session, owner):
    _run(session, status=RunStatus.READY, trigger=SessionOrigin.API_JOB, prompt="Upgrade Django")
    Session.objects.filter(pk=session.pk).update(last_active_at=timezone.now())
    session.refresh_from_db()

    assert _payload(session, owner) is None


def test_an_inbox_run_the_conversation_shows_as_waiting_is_left_out(session, owner):
    next_run = _run(session, trigger=SessionOrigin.MR_WEBHOOK, user=owner)
    make_event(session, EventKind.MENTION, run=next_run, author=owner, merge_request_iid=104, payload=mention_payload())
    Session.objects.filter(pk=session.pk).update(last_active_at=timezone.now())
    session.refresh_from_db()

    assert _payload(session, owner) is None


def test_a_request_that_follows_the_session_is_on_its_new_branch(session, owner):
    _busy(session)
    next_run = _run(session, user=owner)
    chat = make_event(session, EventKind.CHAT_MESSAGE, run=next_run, author=owner)
    Session.objects.filter(pk=session.pk).update(ref="feat/y")
    session.refresh_from_db()
    ci = make_event(session, EventKind.CI_PASSED, ref="feat/y", merge_request_iid=7)

    payload = _payload(session, owner)

    (group,) = payload["groups"]
    assert [bubble["id"] for bubble in group["bubbles"]] == [f"inbox-{chat.pk}"]
    assert [line["id"] for line in group["told"]] == [f"inbox-{ci.pk}"]
    assert payload["told"] == []


def test_a_mention_from_an_unlinked_account_names_its_author(session, owner):
    _busy(session)
    next_run = _run(session, trigger=SessionOrigin.MR_WEBHOOK, external_provider="gitlab", external_uid="77")
    make_event(
        session, EventKind.MENTION, run=next_run, author_username="carol", author_provider="gitlab", author_uid="77",
        merge_request_iid=104, payload=mention_payload(),
    )  # fmt: skip

    (group,) = _payload(session, owner)["groups"]

    assert (group["run_id"], group["who"], group["is_viewer"]) == (str(next_run.pk), "carol", False)
