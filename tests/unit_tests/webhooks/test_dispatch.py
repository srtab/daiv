import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest
from django_tasks_db.models import DBTaskResult
from github.GithubException import GithubException
from gitlab.exceptions import GitlabError
from sessions.models import Run, Session
from webhooks.dispatch import Author, Mention, dispatch_issue_run, dispatch_mr_comment_run

from codebase.base import GitPlatform, Scope
from codebase.clients.base import Emoji
from codebase.utils import compute_thread_id
from tests.unit_tests.webhooks.conftest import make_db_task_result

AUTHOR = Author(provider=GitPlatform.GITLAB, uid=2, username="alice", email="alice@example.com")


def _issue_run(steps, **kwargs):
    return dispatch_issue_run(
        client=steps.client,
        repo_id="group/repo",
        issue_iid=7,
        title="Bug",
        author=AUTHOR,
        **{"use_max": False, **kwargs},
    )


def _mr_comment_run(steps, **kwargs):
    return dispatch_mr_comment_run(
        client=steps.client,
        repo_id="group/repo",
        merge_request_iid=7,
        ref="feat",
        title="MR",
        author=AUTHOR,
        mention=Mention(note_id=300, comment_id="300"),
        **{"use_max": False, **kwargs},
    )


each_run_kind = pytest.mark.parametrize(
    ("dispatch", "task", "scope"),
    [
        pytest.param(_issue_run, "address_issue_task", Scope.ISSUE, id="issue"),
        pytest.param(_mr_comment_run, "address_mr_comments_task", Scope.MERGE_REQUEST, id="merge_request"),
    ],
)


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
        patch("webhooks.dispatch._is_repeated_trigger", new=AsyncMock(return_value=False)),
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
    steps.client.create_merge_request_note_emoji.assert_called_once_with("owner/repo", 99, Emoji.EYES, 300)
    assert steps.address_mr_comments_task.aenqueue.call_args.kwargs["mention_note_id"] == 300
    assert steps.acreate_run.call_args.kwargs["ref"] == "feat/x"


async def test_a_known_source_branch_is_recorded_without_a_fetch(steps):
    await _mr_comment_run(steps)

    steps.client.get_merge_request.assert_not_called()
    assert steps.acreate_run.call_args.kwargs["ref"] == "feat"


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


@each_run_kind
async def test_the_task_and_the_run_share_the_entitys_thread(steps, dispatch, task, scope):
    await dispatch(steps)

    expected = compute_thread_id(repo_slug="group/repo", scope=scope, entity_iid=7)
    assert getattr(steps, task).aenqueue.call_args.kwargs["thread_id"] == expected
    assert steps.acreate_run.call_args.kwargs["thread_id"] == expected


@each_run_kind
@pytest.mark.parametrize(("env", "env_id"), [(Mock(id="env-1"), "env-1"), (None, None)], ids=["env", "no_env"])
async def test_the_task_and_the_run_get_the_repos_sandbox_env(steps, dispatch, task, scope, env, env_id):
    with patch("webhooks.dispatch.resolve_env_for_run", new=AsyncMock(return_value=env)) as resolve_env:
        await dispatch(steps)

    resolve_env.assert_awaited_once_with(user=None, repo_id="group/repo")
    assert getattr(steps, task).aenqueue.call_args.kwargs["sandbox_environment_id"] == env_id
    assert steps.acreate_run.call_args.kwargs["sandbox_environment_id"] == env_id


@each_run_kind
@pytest.mark.parametrize("use_max", [True, False])
async def test_the_task_runs_in_the_max_mode_stamped_on_the_run(steps, dispatch, task, scope, use_max):
    await dispatch(steps, use_max=use_max)

    assert getattr(steps, task).aenqueue.call_args.kwargs["use_max"] is use_max
    assert steps.acreate_run.call_args.kwargs["use_max"] is use_max


@each_run_kind
@pytest.mark.parametrize(
    "exc",
    [GithubException(403, {}, {}), GitlabError("forbidden"), ConnectionError("dns")],
    ids=["github", "gitlab", "transport"],
)
async def test_a_failed_reaction_is_logged_and_the_run_still_starts(steps, caplog, dispatch, task, scope, exc):
    steps.client.create_issue_emoji.side_effect = exc
    steps.client.create_merge_request_note_emoji.side_effect = exc

    with caplog.at_level(logging.WARNING, logger="daiv.webhooks"):
        await dispatch(steps)

    getattr(steps, task).aenqueue.assert_awaited_once()
    steps.acreate_run.assert_awaited_once()
    record = next(r for r in caplog.records if "reaction" in r.message)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None


async def test_a_failed_run_insert_is_logged_not_raised(steps, caplog):
    steps.acreate_run.side_effect = RuntimeError("duplicate task result")

    with caplog.at_level(logging.ERROR, logger="daiv.webhooks"):
        await dispatch_issue_run(
            client=steps.client, repo_id="group/repo", issue_iid=7, title="Bug", use_max=False, author=AUTHOR
        )

    steps.address_issue_task.aenqueue.assert_awaited_once()
    assert "Failed to create run" in caplog.text


@pytest.mark.django_db(transaction=True)
async def test_a_repeated_trigger_reuses_the_first_run_even_when_max_mode_changed(database_task_backend, caplog):
    client = Mock()
    mention = Mention(note_id=200, comment_id="discussion-1")

    with (
        patch("webhooks.dispatch.resolve_user", new=AsyncMock(return_value=None)),
        patch("webhooks.dispatch.resolve_env_for_run", new=AsyncMock(return_value=None)),
        caplog.at_level(logging.INFO, logger="daiv.webhooks"),
    ):
        for use_max in (False, True):
            await dispatch_issue_run(
                client=client,
                repo_id="group/repo",
                issue_iid=7,
                title="Bug",
                use_max=use_max,
                author=AUTHOR,
                mention=mention,
            )

    assert await DBTaskResult.objects.acount() == 1
    assert await Run.objects.acount() == 1
    assert "skipping the repeated trigger" in caplog.text
    assert "Failed to create run" not in caplog.text


@pytest.mark.django_db(transaction=True)
async def test_two_triggers_on_one_issue_share_its_session():
    task_ids = [await make_db_task_result(), await make_db_task_result()]
    thread_id = compute_thread_id(repo_slug="group/repo", scope=Scope.ISSUE, entity_iid=7)

    with (
        patch("webhooks.dispatch.address_issue_task") as task,
        patch("webhooks.dispatch.resolve_user", new=AsyncMock(return_value=None)),
        patch("webhooks.dispatch.resolve_env_for_run", new=AsyncMock(return_value=None)),
    ):
        task.aenqueue = AsyncMock(side_effect=[Mock(id=task_id) for task_id in task_ids])
        for note_id in (200, 201):
            await dispatch_issue_run(
                client=Mock(),
                repo_id="group/repo",
                issue_iid=7,
                title="Bug",
                use_max=False,
                author=AUTHOR,
                mention=Mention(note_id=note_id, comment_id="discussion-1"),
            )

    assert await Session.objects.filter(thread_id=thread_id).acount() == 1
    assert await Run.objects.filter(session_id=thread_id).acount() == 2
