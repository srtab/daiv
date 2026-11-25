from unittest.mock import AsyncMock, Mock, patch

import pytest
from django_tasks_db.models import DBTaskResult, get_date_max
from sessions.inbox.kinds import SourceKey
from sessions.merge_request_runs.starter import Caller, Request, RunStarter
from sessions.merge_request_runs.tasks import address_merge_request_task
from sessions.models import Run, SessionOrigin

from accounts.models import User
from codebase.base import GitPlatform, Scope


def test_a_users_caller_names_them_alone():
    user = User(pk=4, username="alice")

    assert Caller(user=user).run_fields == {
        "user": user,
        "external_username": "",
        "external_provider": "",
        "external_uid": "",
        "acts_as_user": True,
    }


def test_a_platform_accounts_caller_names_the_account():
    caller = Caller(user=None, provider=GitPlatform.GITHUB, uid=12, username="octo")

    assert caller.run_fields == {
        "user": None,
        "external_username": "octo",
        "external_provider": GitPlatform.GITHUB,
        "external_uid": "12",
        "acts_as_user": True,
    }


def test_a_caller_acts_as_its_user_only_when_it_says_so():
    user = User(pk=4, username="alice")

    assert (Caller(user=user).acting_user, Caller(user=user, acts_as_user=False).acting_user) == (user, None)


@pytest.mark.django_db
async def test_a_given_model_and_environment_replace_the_defaults():
    task = Mock()
    task.get_or_enqueue.return_value = (Mock(id="task-1"), True)
    starter = RunStarter(repo_id="group/repo", scope=Scope.MERGE_REQUEST, iid=7)

    with (
        patch("sessions.merge_request_runs.starter.create_run") as create_run,
        patch("sessions.merge_request_runs.starter.resolve_env_for_run", new=AsyncMock()) as resolve_env,
    ):
        await starter.astart(
            task,
            Caller(user=None),
            use_max=True,
            task_kwargs={},
            trigger_type="api_job",
            title="t",
            reply=None,
            request=Request(
                prompt="p",
                trigger_type="api_job",
                request_key="job:1",
                agent_model="openrouter:x",
                agent_thinking_level="low",
                sandbox_environment_id="env-1",
            ),
        )

    resolve_env.assert_not_awaited()
    assert task.get_or_enqueue.call_args.kwargs["sandbox_environment_id"] == "env-1"
    kwargs = create_run.call_args.kwargs
    assert (kwargs["agent_model"], kwargs["agent_thinking_level"], kwargs["sandbox_environment_id"]) == (
        "openrouter:x",
        "low",
        "env-1",
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("database_task_backend")
@pytest.mark.parametrize(
    ("status", "note_id", "on_diff", "started"),
    [
        ("READY", 5, False, False),
        ("RUNNING", 5, False, False),
        ("SUCCESSFUL", 5, False, False),
        ("FAILED", 5, False, True),
        ("READY", 6, False, True),
        ("READY", 5, True, True),
    ],
)
async def test_a_mention_queued_under_its_old_task_path_starts_no_second_run(status, note_id, on_diff, started):
    await DBTaskResult.objects.acreate(
        status=status,
        task_path="webhooks.tasks.address_mr_comments_task",
        args_kwargs={
            "args": [],
            "kwargs": {"repo_id": "group/repo", "merge_request_id": 7, "mention_comment_id": "d", "mention_note_id": 5},
        },
        queue_name="default",
        backend_name="default",
        run_after=get_date_max(),
        return_value={},
    )
    starter = RunStarter(repo_id="group/repo", scope=Scope.MERGE_REQUEST, iid=7)

    with patch("sessions.merge_request_runs.starter.resolve_env_for_run", new=AsyncMock(return_value=None)):
        outcome = await starter.astart(
            address_merge_request_task,
            Caller(user=None, username="bob"),
            use_max=False,
            task_kwargs={
                "merge_request_id": 7,
                "request_key": SourceKey.note(note_id),
                "mention_comment_id": "d",
                "mention_note_id": note_id,
                "mention_on_diff": on_diff,
            },
            trigger_type=SessionOrigin.MR_WEBHOOK,
            title="t",
            reply=None,
        )

    assert outcome.started is started
    assert await Run.objects.aexists() is started
