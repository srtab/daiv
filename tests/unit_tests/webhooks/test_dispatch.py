import asyncio
import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest
from django_tasks import TaskResultStatus
from django_tasks_db.models import DBTaskResult
from github.GithubException import GithubException
from gitlab.exceptions import GitlabError
from sessions.ids import compute_thread_id
from sessions.models import Run, Session
from webhooks.dispatch import Author, Mention, dispatch_issue_run, dispatch_mr_comment_run

from codebase.base import GitPlatform, Scope
from codebase.clients.base import Emoji
from core.site_settings import site_settings
from tests.unit_tests.webhooks.conftest import make_db_task_result

pytestmark = pytest.mark.django_db

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
        pytest.param(_issue_run, "address_issue_label_task", Scope.ISSUE, id="issue"),
        pytest.param(_mr_comment_run, "address_mr_comments_task", Scope.MERGE_REQUEST, id="merge_request"),
    ],
)


@pytest.fixture
def steps():
    """One mock recording the branch fetch, enqueue, Run insert and reaction in the order they happen."""
    steps = Mock()
    for task in (steps.address_issue_task, steps.address_issue_label_task, steps.address_mr_comments_task):
        task.get_or_enqueue.return_value = (Mock(id="task-1"), True)
    with (
        patch("webhooks.dispatch.address_issue_task", steps.address_issue_task),
        patch("webhooks.dispatch.address_issue_label_task", steps.address_issue_label_task),
        patch("webhooks.dispatch.address_mr_comments_task", steps.address_mr_comments_task),
        patch("webhooks.dispatch.create_run", steps.create_run),
        patch("webhooks.dispatch.resolve_user", new=AsyncMock(return_value=None)),
        patch("webhooks.dispatch.resolve_env_for_run", new=AsyncMock(return_value=None)),
    ):
        yield steps


async def test_a_label_trigger_records_the_run_with_its_task_then_reacts(steps):
    await dispatch_issue_run(
        client=steps.client, repo_id="group/repo", issue_iid=7, title="Bug", use_max=False, author=AUTHOR
    )

    assert [name for name, *_ in steps.mock_calls] == [
        "address_issue_label_task.get_or_enqueue",
        "create_run",
        "client.create_issue_emoji",
    ]
    steps.client.create_issue_emoji.assert_called_once_with("group/repo", 7, Emoji.EYES, None)
    task_kwargs = steps.address_issue_label_task.get_or_enqueue.call_args.kwargs
    assert task_kwargs["mention_comment_id"] is None
    assert task_kwargs["mention_note_id"] is None
    run_kwargs = steps.create_run.call_args.kwargs
    assert run_kwargs["thread_id"] == task_kwargs["thread_id"]
    assert run_kwargs["issue_iid"] == 7
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
    assert steps.address_issue_task.get_or_enqueue.call_args.kwargs["mention_comment_id"] == "discussion-1"
    assert steps.address_issue_task.get_or_enqueue.call_args.kwargs["mention_note_id"] == 200
    assert steps.create_run.call_args.kwargs["mention_comment_id"] == "discussion-1"


async def test_a_missing_source_branch_is_fetched_before_the_run_is_recorded(steps):
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
        "client.get_merge_request",
        "address_mr_comments_task.get_or_enqueue",
        "create_run",
        "client.create_merge_request_note_emoji",
    ]
    steps.client.create_merge_request_note_emoji.assert_called_once_with("owner/repo", 99, Emoji.EYES, 300)
    assert steps.address_mr_comments_task.get_or_enqueue.call_args.kwargs["mention_note_id"] == 300
    assert steps.create_run.call_args.kwargs["ref"] == "feat/x"


async def test_a_known_source_branch_is_recorded_without_a_fetch(steps):
    await _mr_comment_run(steps)

    steps.client.get_merge_request.assert_not_called()
    assert steps.create_run.call_args.kwargs["ref"] == "feat"


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

    assert steps.create_run.call_args.kwargs["ref"] == ""
    assert "source branch" in caplog.text


@each_run_kind
async def test_the_task_and_the_run_share_the_entitys_thread(steps, dispatch, task, scope):
    await dispatch(steps)

    expected = compute_thread_id(repo_slug="group/repo", scope=scope, entity_iid=7)
    assert getattr(steps, task).get_or_enqueue.call_args.kwargs["thread_id"] == expected
    assert steps.create_run.call_args.kwargs["thread_id"] == expected


@each_run_kind
@pytest.mark.parametrize(("env", "env_id"), [(Mock(id="env-1"), "env-1"), (None, None)], ids=["env", "no_env"])
async def test_the_task_and_the_run_get_the_repos_sandbox_env(steps, dispatch, task, scope, env, env_id):
    with patch("webhooks.dispatch.resolve_env_for_run", new=AsyncMock(return_value=env)) as resolve_env:
        await dispatch(steps)

    resolve_env.assert_awaited_once_with(user=None, repo_id="group/repo")
    assert getattr(steps, task).get_or_enqueue.call_args.kwargs["sandbox_environment_id"] == env_id
    assert steps.create_run.call_args.kwargs["sandbox_environment_id"] == env_id


@each_run_kind
@pytest.mark.parametrize("use_max", [True, False])
async def test_the_task_runs_in_the_max_mode_whose_model_the_run_records(steps, dispatch, task, scope, use_max):
    await dispatch(steps, use_max=use_max)

    assert getattr(steps, task).get_or_enqueue.call_args.kwargs["use_max"] is use_max
    run_kwargs = steps.create_run.call_args.kwargs
    if use_max:
        assert run_kwargs["agent_model"] == site_settings.agent_max_model_name
        assert run_kwargs["agent_thinking_level"] == site_settings.agent_max_thinking_level
    else:
        assert (run_kwargs["agent_model"], run_kwargs["agent_thinking_level"]) == ("", "")


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

    getattr(steps, task).get_or_enqueue.assert_called_once()
    steps.create_run.assert_called_once()
    record = next(r for r in caplog.records if "reaction" in r.message)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None


async def test_a_repeat_records_no_run_and_does_not_react(steps):
    steps.address_mr_comments_task.get_or_enqueue.return_value = (Mock(id="task-1"), False)

    await dispatch_mr_comment_run(
        client=steps.client,
        repo_id="owner/repo",
        merge_request_iid=99,
        ref="feat/x",
        title="PR",
        use_max=False,
        author=AUTHOR,
        mention=Mention(note_id=300, comment_id="300"),
    )

    assert [name for name, *_ in steps.mock_calls] == ["address_mr_comments_task.get_or_enqueue"]


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
async def test_a_repeated_trigger_reuses_the_first_run_even_when_max_mode_changed():
    for use_max in (False, True):
        await _dispatch_mention(use_max=use_max)

    assert await DBTaskResult.objects.acount() == 1
    assert await Run.objects.acount() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("real_dispatch")
async def test_concurrent_deliveries_of_one_mention_record_one_run():
    await asyncio.gather(_dispatch_mention(), _dispatch_mention())

    run = await Run.objects.aget()
    assert await DBTaskResult.objects.acount() == 1
    assert str(run.task_result_id) == str((await DBTaskResult.objects.aget()).id)
    assert await Session.objects.acount() == 1


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


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("real_dispatch")
async def test_a_failed_run_write_rolls_the_enqueue_back():
    with (
        patch("webhooks.dispatch.create_run", side_effect=RuntimeError("db down")),
        pytest.raises(RuntimeError, match="db down"),
    ):
        await _dispatch_mention()

    assert await DBTaskResult.objects.acount() == 0
    await _dispatch_mention()
    assert await Run.objects.acount() == 1


@pytest.mark.django_db(transaction=True)
async def test_two_triggers_on_one_issue_share_its_session():
    task_ids = [await make_db_task_result(), await make_db_task_result()]
    thread_id = compute_thread_id(repo_slug="group/repo", scope=Scope.ISSUE, entity_iid=7)

    with (
        patch("webhooks.dispatch.address_issue_task") as task,
        patch("webhooks.dispatch.resolve_user", new=AsyncMock(return_value=None)),
        patch("webhooks.dispatch.resolve_env_for_run", new=AsyncMock(return_value=None)),
    ):
        task.get_or_enqueue.side_effect = [(Mock(id=task_id), True) for task_id in task_ids]
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
