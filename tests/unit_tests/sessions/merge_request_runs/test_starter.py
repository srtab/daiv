from unittest.mock import AsyncMock, Mock, patch

import pytest
from sessions.merge_request_runs.starter import Requester, RunStarter

from accounts.models import User
from codebase.base import GitPlatform, Scope


def test_a_users_requester_names_them_alone():
    user = User(pk=4, username="alice")

    assert Requester(user=user).run_fields == {
        "user": user,
        "external_username": "",
        "external_provider": "",
        "external_uid": "",
    }


def test_a_platform_accounts_requester_names_the_account():
    requester = Requester(user=None, provider=GitPlatform.GITHUB, uid=12, username="octo")

    assert requester.run_fields == {
        "user": None,
        "external_username": "octo",
        "external_provider": GitPlatform.GITHUB,
        "external_uid": "12",
    }


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
            Requester(user=None),
            use_max=True,
            task_kwargs={},
            trigger_type="api_job",
            title="t",
            reply=None,
            agent_model="openrouter:x",
            agent_thinking_level="low",
            sandbox_environment_id="env-1",
        )

    resolve_env.assert_not_awaited()
    assert task.get_or_enqueue.call_args.kwargs["sandbox_environment_id"] == "env-1"
    kwargs = create_run.call_args.kwargs
    assert (kwargs["agent_model"], kwargs["agent_thinking_level"], kwargs["sandbox_environment_id"]) == (
        "openrouter:x",
        "low",
        "env-1",
    )
