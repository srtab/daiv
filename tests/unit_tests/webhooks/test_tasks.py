from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from webhooks.tasks import address_issue_task, address_mr_comments_task

from codebase.base import MergeRequest, User
from codebase.exceptions import CloneRefNotFoundError

_CONTEXT = SimpleNamespace(task_result=SimpleNamespace(id="tr-1"))


@pytest.fixture(autouse=True)
def run_lookup():
    """The Run a webhook linked to ``_CONTEXT``'s task result, stubbed so these tests need no database."""
    with patch("webhooks.tasks.aget_task_run_id", AsyncMock(return_value="run-1")) as lookup:
        yield lookup


def _mr(*, merged: bool) -> MergeRequest:
    return MergeRequest(
        repo_id="group/repo",
        merge_request_id=7,
        source_branch="chore/x",
        target_branch="dev",
        title="t",
        description="d",
        web_url="https://git/group/repo/-/merge_requests/7",
        author=User(id=1, username="u", name="U"),
        merged=merged,
    )


async def test_address_mr_comments_skips_when_merged(run_lookup):
    client = MagicMock()
    client.get_merge_request.return_value = _mr(merged=True)

    with (
        patch("webhooks.tasks.RepoClient.create_instance", return_value=client),
        patch("webhooks.managers.review_addressor.CommentsAddressorManager.address_comments") as address,
    ):
        result = await address_mr_comments_task.func(
            _CONTEXT, repo_id="group/repo", merge_request_id=7, mention_comment_id="d1"
        )

    address.assert_not_called()
    run_lookup.assert_not_awaited()
    client.create_merge_request_comment.assert_called_once()
    assert "already been merged" in result["response"]
    assert result["code_changes"] is False


async def test_address_mr_comments_skips_when_branch_gone():
    client = MagicMock()
    client.get_merge_request.return_value = _mr(merged=False)

    with (
        patch("webhooks.tasks.RepoClient.create_instance", return_value=client),
        patch(
            "webhooks.managers.review_addressor.CommentsAddressorManager.address_comments",
            AsyncMock(side_effect=CloneRefNotFoundError("chore/x", "group/repo")),
        ),
    ):
        result = await address_mr_comments_task.func(
            _CONTEXT, repo_id="group/repo", merge_request_id=7, mention_comment_id="d1"
        )

    client.create_merge_request_comment.assert_called_once()
    assert "no longer exists" in result["response"]
    assert result["code_changes"] is False


async def test_address_mr_comments_hands_the_merge_request_and_its_run_to_the_addressor(run_lookup):
    client = MagicMock()
    merge_request = _mr(merged=False)
    client.get_merge_request.return_value = merge_request
    address = AsyncMock(return_value={"response": "done"})

    with (
        patch("webhooks.tasks.RepoClient.create_instance", return_value=client),
        patch("webhooks.managers.review_addressor.CommentsAddressorManager.address_comments", address),
    ):
        result = await address_mr_comments_task.func(
            _CONTEXT,
            repo_id="group/repo",
            merge_request_id=7,
            mention_comment_id="d1",
            thread_id="t-7",
            sandbox_environment_id="e",
        )

    assert result == {"response": "done"}
    assert address.await_args.kwargs == {
        "repo_id": "group/repo",
        "merge_request": merge_request,
        "mention_comment_id": "d1",
        "thread_id": "t-7",
        "sandbox_env_id": "e",
        "run_id": "run-1",
    }
    run_lookup.assert_awaited_once_with("tr-1")


class TestAddressIssueTaskRef:
    """An issue webhook carries no ref of its own — an issue is not a branch. ``Session.ref`` is what carries an
    issue session's working branch from one turn to the next: without it a follow-up turn re-clones the default
    branch while the checkpoint still names the branch the previous turn published to, and the publish then pushes
    a sibling commit onto it. The re-pin after a vanished branch is the executor's (``TestRefFallback`` in
    ``tests/unit_tests/sessions/executor/test_run.py``)."""

    @staticmethod
    async def _addressed(*, session_ref: str, ref: str | None = None) -> AsyncMock:
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
                thread_id="t-1",
                ref=ref,
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

    async def test_an_explicit_ref_wins_over_the_session(self):
        addressed = await self._addressed(session_ref="fix/10", ref="release/1.2")
        assert addressed.await_args.kwargs["ref"] == "release/1.2"

    async def test_the_task_hands_the_issue_and_its_run_to_the_addressor(self, run_lookup):
        addressed = await self._addressed(session_ref="")
        assert addressed.await_args.kwargs == {
            "repo_id": "group/repo",
            "issue": addressed.issue,
            "mention_comment_id": "d1",
            "ref": None,
            "thread_id": "t-1",
            "sandbox_env_id": "e",
            "run_id": "run-1",
        }
        run_lookup.assert_awaited_once_with("tr-1")
