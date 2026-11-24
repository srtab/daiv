from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import webhooks.tasks as webhooks_tasks
from sessions.services import TaskRun
from webhooks.tasks import address_issue_task, address_mr_comments_task

_CONTEXT = SimpleNamespace(task_result=SimpleNamespace(id="tr-1"))


@pytest.fixture(autouse=True)
def run_lookup():
    """The Run a webhook linked to ``_CONTEXT``'s task result, stubbed so these tests need no database."""
    with patch("webhooks.tasks.aget_task_run", AsyncMock(return_value=TaskRun("run-1", 5))) as lookup:
        yield lookup


async def test_setup_webhooks_cron_task_calls_command():
    if not hasattr(webhooks_tasks, "setup_webhooks_cron_task"):
        pytest.skip("setup_webhooks_cron_task is only defined for the GitLab client")

    with patch("webhooks.tasks.call_command") as mock_call_command, patch("webhooks.tasks.django_settings.DEBUG", True):
        await webhooks_tasks.setup_webhooks_cron_task.aenqueue()

    mock_call_command.assert_called_once_with("setup_webhooks", disable_ssl_verification=True)


async def test_the_old_merge_request_task_path_forwards_to_the_moved_task():
    forwarded = AsyncMock(return_value={"response": "done"})

    with patch("webhooks.tasks.address_merge_request_task", SimpleNamespace(func=forwarded)):
        result = await address_mr_comments_task.func(
            _CONTEXT,
            repo_id="group/repo",
            merge_request_id=7,
            mention_comment_id="d1",
            thread_id="t-7",
            use_max=True,
            mention_note_id=5,
            sandbox_environment_id="e",
        )

    assert result == {"response": "done"}
    forwarded.assert_awaited_once_with(
        _CONTEXT,
        repo_id="group/repo",
        merge_request_id=7,
        thread_id="t-7",
        use_max=True,
        request_key="note:5",
        mention_comment_id="d1",
        mention_note_id=5,
        sandbox_environment_id="e",
    )


class TestAddressIssueTaskRef:
    """An issue webhook carries no ref of its own — an issue is not a branch. ``Session.ref`` is what carries an
    issue session's working branch from one turn to the next: without it a follow-up turn re-clones the default
    branch while the checkpoint still names the branch the previous turn published to, and the publish then pushes
    a sibling commit onto it. The re-pin after a vanished branch is the executor's (``TestRefFallback`` in
    ``tests/unit_tests/sessions/executor/test_run.py``)."""

    @staticmethod
    async def _addressed(*, session_ref: str) -> AsyncMock:
        client = MagicMock()
        client.get_issue.return_value = MagicMock()
        addressed = AsyncMock(return_value={"response": "", "code_changes": False})
        with (
            patch("webhooks.tasks.RepoClient.create_instance", return_value=client),
            patch("webhooks.tasks.aget_session_ref", AsyncMock(return_value=session_ref)),
            patch("webhooks.managers.issue_addressor.IssueAddressorManager.address_issue", addressed),
        ):
            await address_issue_task.func(
                _CONTEXT,
                repo_id="group/repo",
                issue_iid=10,
                mention_comment_id="d1",
                mention_note_id=5,
                thread_id="t-1",
                use_max=False,
                sandbox_environment_id="e",
            )

        addressed.issue = client.get_issue.return_value
        return addressed

    async def test_a_follow_up_turn_clones_the_branch_the_session_works_on(self):
        addressed = await self._addressed(session_ref="fix/10-update-dependencies")
        assert addressed.await_args.kwargs["ref"] == "fix/10-update-dependencies"

    async def test_a_first_turn_still_clones_the_default_branch(self):
        """Nothing published yet, so there is no working branch to resume — ``None`` lets the clone pick the
        repository default."""
        addressed = await self._addressed(session_ref="")
        assert addressed.await_args.kwargs["ref"] is None

    async def test_the_task_hands_the_issue_and_its_run_to_the_addressor(self, run_lookup):
        addressed = await self._addressed(session_ref="")
        assert addressed.await_args.kwargs == {
            "repo_id": "group/repo",
            "issue": addressed.issue,
            "mention_comment_id": "d1",
            "mention_note_id": 5,
            "ref": None,
            "thread_id": "t-1",
            "sandbox_env_id": "e",
            "run_id": "run-1",
            "use_max": False,
            "acting_user_id": 5,
        }
        run_lookup.assert_awaited_once_with("tr-1")
