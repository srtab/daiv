import asyncio
import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest
from django_tasks import TaskResultStatus
from django_tasks_db.models import DBTaskResult
from sessions.models import Run, Session
from webhooks.dispatch import Author, Mention, dispatch_issue_run, dispatch_mr_comment_run

from codebase.base import GitPlatform
from codebase.clients.base import Emoji

AUTHOR = Author(provider=GitPlatform.GITLAB, uid=2, username="alice", email="alice@example.com")


@pytest.fixture
def steps():
    """One mock recording the repeat check, reaction, branch fetch, session, enqueue and Run insert in the order they
    happen."""
    steps = Mock()
    steps.aget_or_create_session = AsyncMock()
    for task in (steps.address_issue_task, steps.address_issue_label_task, steps.address_mr_comments_task):
        task.afind_duplicate = AsyncMock(return_value=None)
        task.aget_or_enqueue = AsyncMock(return_value=(Mock(id="task-1"), True))
    steps.acreate_run = AsyncMock()
    with (
        patch("webhooks.dispatch.address_issue_task", steps.address_issue_task),
        patch("webhooks.dispatch.address_issue_label_task", steps.address_issue_label_task),
        patch("webhooks.dispatch.address_mr_comments_task", steps.address_mr_comments_task),
        patch("webhooks.dispatch.aget_or_create_session", steps.aget_or_create_session),
        patch("webhooks.dispatch.acreate_run", steps.acreate_run),
        patch("webhooks.dispatch.resolve_user", new=AsyncMock(return_value=None)),
        patch("webhooks.dispatch.resolve_env_for_run", new=AsyncMock(return_value=None)),
    ):
        yield steps


async def test_a_label_trigger_opens_the_session_before_it_enqueues_then_records_the_run_on_it(steps):
    await dispatch_issue_run(
        client=steps.client, repo_id="group/repo", issue_iid=7, title="Bug", use_max=False, author=AUTHOR
    )

    assert [name for name, *_ in steps.mock_calls] == [
        "address_issue_label_task.afind_duplicate",
        "client.create_issue_emoji",
        "aget_or_create_session",
        "address_issue_label_task.aget_or_enqueue",
        "acreate_run",
    ]
    steps.client.create_issue_emoji.assert_called_once_with("group/repo", 7, Emoji.EYES, None)
    task_kwargs = steps.address_issue_label_task.aget_or_enqueue.call_args.kwargs
    assert steps.address_issue_label_task.afind_duplicate.call_args.kwargs == task_kwargs
    assert task_kwargs["mention_comment_id"] is None
    assert task_kwargs["mention_note_id"] is None
    assert steps.aget_or_create_session.call_args.kwargs["thread_id"] == task_kwargs["thread_id"]
    run_kwargs = steps.acreate_run.call_args.kwargs
    assert run_kwargs["session"] is steps.aget_or_create_session.return_value
    assert run_kwargs["task_result_id"] == "task-1"
    assert run_kwargs["external_username"] == "alice"


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
    assert steps.address_issue_task.aget_or_enqueue.call_args.kwargs["mention_comment_id"] == "discussion-1"
    assert steps.address_issue_task.aget_or_enqueue.call_args.kwargs["mention_note_id"] == 200
    assert steps.acreate_run.call_args.kwargs["mention_comment_id"] == "discussion-1"


async def test_a_missing_source_branch_is_fetched_before_the_session_opens(steps):
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
        "address_mr_comments_task.afind_duplicate",
        "client.create_merge_request_note_emoji",
        "client.get_merge_request",
        "aget_or_create_session",
        "address_mr_comments_task.aget_or_enqueue",
        "acreate_run",
    ]
    assert steps.address_mr_comments_task.aget_or_enqueue.call_args.kwargs["mention_note_id"] == 300
    assert steps.aget_or_create_session.call_args.kwargs["ref"] == "feat/x"
    assert steps.acreate_run.call_args.kwargs["ref"] == "feat/x"


async def test_a_failed_source_branch_fetch_still_starts_the_run(steps, caplog):
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


async def test_a_failed_session_write_raises_before_anything_is_queued(steps):
    steps.aget_or_create_session.side_effect = RuntimeError("db down")

    with pytest.raises(RuntimeError, match="db down"):
        await dispatch_issue_run(
            client=steps.client, repo_id="group/repo", issue_iid=7, title="Bug", use_max=False, author=AUTHOR
        )

    steps.address_issue_label_task.aget_or_enqueue.assert_not_awaited()


async def test_a_failed_run_insert_is_logged_not_raised(steps, caplog):
    steps.acreate_run.side_effect = RuntimeError("duplicate task result")

    with caplog.at_level(logging.ERROR, logger="daiv.webhooks"):
        await dispatch_issue_run(
            client=steps.client, repo_id="group/repo", issue_iid=7, title="Bug", use_max=False, author=AUTHOR
        )

    steps.address_issue_label_task.aget_or_enqueue.assert_awaited_once()
    assert "Failed to create run" in caplog.text


async def test_a_repeat_found_up_front_reacts_on_and_writes_nothing(steps, caplog):
    steps.address_mr_comments_task.afind_duplicate.return_value = Mock(id="task-1")

    with caplog.at_level(logging.INFO, logger="daiv.webhooks"):
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

    assert [name for name, *_ in steps.mock_calls] == ["address_mr_comments_task.afind_duplicate"]
    assert "skipping the repeated trigger" in caplog.text


async def test_a_task_the_enqueue_did_not_create_gets_no_second_run(steps, caplog):
    steps.address_issue_label_task.aget_or_enqueue.return_value = (Mock(id="task-1"), False)

    with caplog.at_level(logging.INFO, logger="daiv.webhooks"):
        await dispatch_issue_run(
            client=steps.client, repo_id="group/repo", issue_iid=7, title="Bug", use_max=False, author=AUTHOR
        )

    steps.acreate_run.assert_not_awaited()
    assert "skipping the repeated trigger" in caplog.text


@pytest.fixture
def real_dispatch(database_task_backend):
    """Dispatch against the database task backend, with only the platform and user lookups stubbed."""
    with (
        patch("webhooks.dispatch.resolve_user", new=AsyncMock(return_value=None)),
        patch("webhooks.dispatch.resolve_env_for_run", new=AsyncMock(return_value=None)),
    ):
        yield


def _dispatch_mention(*, use_max: bool = False):
    return dispatch_issue_run(
        client=Mock(),
        repo_id="group/repo",
        issue_iid=7,
        title="Bug",
        use_max=use_max,
        author=AUTHOR,
        mention=Mention(note_id=200, comment_id="discussion-1"),
    )


def _dispatch_label():
    return dispatch_issue_run(
        client=Mock(), repo_id="group/repo", issue_iid=7, title="Bug", use_max=False, author=AUTHOR
    )


async def _finish_every_task() -> None:
    await DBTaskResult.objects.aupdate(status=TaskResultStatus.SUCCESSFUL)


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("real_dispatch")
async def test_a_repeated_trigger_reuses_the_first_run_even_when_max_mode_changed(caplog):
    with caplog.at_level(logging.INFO, logger="daiv.webhooks"):
        for use_max in (False, True):
            await _dispatch_mention(use_max=use_max)

    assert await DBTaskResult.objects.acount() == 1
    assert await Run.objects.acount() == 1
    assert "skipping the repeated trigger" in caplog.text
    assert "Failed to create run" not in caplog.text


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("real_dispatch")
async def test_concurrent_deliveries_of_one_mention_record_one_run(caplog):
    with caplog.at_level(logging.INFO, logger="daiv.webhooks"):
        await asyncio.gather(_dispatch_mention(), _dispatch_mention())

    run = await Run.objects.aget()
    assert await DBTaskResult.objects.acount() == 1
    assert str(run.task_result_id) == str((await DBTaskResult.objects.aget()).id)
    assert await Session.objects.acount() == 1
    assert "skipping the repeated trigger" in caplog.text
    assert "Failed to create run" not in caplog.text


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("real_dispatch")
async def test_a_mention_repeated_after_its_run_finished_still_reuses_it():
    await _dispatch_mention()
    await _finish_every_task()

    await _dispatch_mention()

    assert await Run.objects.acount() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("real_dispatch")
async def test_a_label_trigger_repeats_a_queued_run_but_not_a_finished_one():
    await _dispatch_label()
    await _dispatch_label()
    assert await Run.objects.acount() == 1

    await _finish_every_task()
    await _dispatch_label()

    assert await Run.objects.acount() == 2
