import logging
from unittest.mock import AsyncMock, Mock, patch

from django.db import IntegrityError

import pytest
from webhooks.dispatch import Author, Mention, dispatch_issue_run, dispatch_mr_comment_run

from codebase.clients.base import Emoji

AUTHOR = Author(provider="gitlab", uid=2, username="alice", email="alice@example.com")


@pytest.fixture
def steps():
    """One mock recording the reaction, enqueue, branch fetch and Run insert in the order they happen."""
    steps = Mock()
    steps.address_issue_task.aenqueue = AsyncMock(return_value=Mock(id="task-1"))
    steps.address_mr_comments_task.aenqueue = AsyncMock(return_value=Mock(id="task-1"))
    steps.acreate_run = AsyncMock()
    with (
        patch("webhooks.dispatch.address_issue_task", steps.address_issue_task),
        patch("webhooks.dispatch.address_mr_comments_task", steps.address_mr_comments_task),
        patch("webhooks.dispatch.acreate_run", steps.acreate_run),
        patch("webhooks.dispatch.resolve_user", new=AsyncMock(return_value=None)),
        patch("webhooks.dispatch.resolve_env_for_run", new=AsyncMock(return_value=None)),
    ):
        yield steps


async def test_a_label_trigger_reacts_then_enqueues_without_a_mention_then_records_the_run(steps):
    await dispatch_issue_run(
        client=steps.client, repo_id="group/repo", issue_iid=7, title="Bug", use_max=False, author=AUTHOR
    )

    assert [name for name, *_ in steps.mock_calls] == [
        "client.create_issue_emoji",
        "address_issue_task.aenqueue",
        "acreate_run",
    ]
    steps.client.create_issue_emoji.assert_called_once_with("group/repo", 7, Emoji.EYES, None)
    task_kwargs = steps.address_issue_task.aenqueue.call_args.kwargs
    assert task_kwargs["mention_comment_id"] is None
    assert task_kwargs["mention_note_id"] is None
    assert steps.acreate_run.call_args.kwargs["task_result_id"] == "task-1"
    assert steps.acreate_run.call_args.kwargs["external_username"] == "alice"


async def test_a_mention_reacts_on_its_note_answers_its_comment_and_dedups_on_its_note(steps):
    await dispatch_issue_run(
        client=steps.client,
        repo_id="group/repo",
        issue_iid=7,
        title="Bug",
        use_max=False,
        author=AUTHOR,
        mention=Mention(note_id=200, comment_id="discussion-1"),
    )

    steps.client.create_issue_emoji.assert_called_once_with("group/repo", 7, Emoji.EYES, 200)
    assert steps.address_issue_task.aenqueue.call_args.kwargs["mention_comment_id"] == "discussion-1"
    assert steps.address_issue_task.aenqueue.call_args.kwargs["mention_note_id"] == 200
    assert steps.acreate_run.call_args.kwargs["mention_comment_id"] == "discussion-1"


async def test_a_missing_source_branch_is_fetched_after_the_enqueue(steps):
    steps.client.get_merge_request.return_value = Mock(source_branch="feat/x")

    await dispatch_mr_comment_run(
        client=steps.client,
        repo_id="owner/repo",
        merge_request_iid=99,
        ref=None,
        title="PR",
        use_max=False,
        author=AUTHOR,
        mention=Mention(note_id=300, comment_id="300"),
    )

    assert [name for name, *_ in steps.mock_calls] == [
        "client.create_merge_request_note_emoji",
        "address_mr_comments_task.aenqueue",
        "client.get_merge_request",
        "acreate_run",
    ]
    assert steps.address_mr_comments_task.aenqueue.call_args.kwargs["mention_note_id"] == 300
    assert steps.acreate_run.call_args.kwargs["ref"] == "feat/x"


async def test_a_failed_source_branch_fetch_still_records_the_run(steps, caplog):
    steps.client.get_merge_request.side_effect = RuntimeError("api down")

    with caplog.at_level(logging.ERROR, logger="daiv.webhooks"):
        await dispatch_mr_comment_run(
            client=steps.client,
            repo_id="owner/repo",
            merge_request_iid=99,
            ref=None,
            title="PR",
            use_max=False,
            author=AUTHOR,
            mention=Mention(note_id=300, comment_id="300"),
        )

    assert steps.acreate_run.call_args.kwargs["ref"] == ""
    assert "source branch" in caplog.text


async def test_a_failed_run_insert_is_logged_not_raised(steps, caplog):
    steps.acreate_run.side_effect = RuntimeError("duplicate task result")

    with caplog.at_level(logging.ERROR, logger="daiv.webhooks"):
        await dispatch_issue_run(
            client=steps.client, repo_id="group/repo", issue_iid=7, title="Bug", use_max=False, author=AUTHOR
        )

    steps.address_issue_task.aenqueue.assert_awaited_once()
    assert "Failed to create run" in caplog.text


@pytest.mark.parametrize(("run_exists", "level"), [(True, logging.INFO), (False, logging.ERROR)])
async def test_a_task_result_that_already_has_its_run_is_a_repeated_trigger(steps, caplog, run_exists, level):
    steps.acreate_run.side_effect = IntegrityError("UNIQUE constraint failed: agent_sessions_run.task_result_id")

    with (
        patch("webhooks.dispatch.Run.objects.filter", return_value=Mock(aexists=AsyncMock(return_value=run_exists))),
        caplog.at_level(logging.INFO, logger="daiv.webhooks"),
    ):
        await dispatch_issue_run(
            client=steps.client, repo_id="group/repo", issue_iid=7, title="Bug", use_max=False, author=AUTHOR
        )

    assert [record.levelno for record in caplog.records] == [level]
