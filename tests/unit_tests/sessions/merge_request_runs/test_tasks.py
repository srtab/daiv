from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.utils import timezone

import pytest
from sessions.legs import NOT_RUN_ARCHIVED
from sessions.merge_request_runs.tasks import address_merge_request_task
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.services import TaskRun

from codebase.base import MergeRequest, User
from codebase.exceptions import CloneRefNotFoundError

_CONTEXT = SimpleNamespace(task_result=SimpleNamespace(id="tr-1"))
_ADDRESS = "sessions.merge_request_runs.addressor.CommentsAddressorManager.address_comments"


@pytest.fixture(autouse=True)
def run_lookup():
    """The Run linked to ``_CONTEXT``'s task result, stubbed so these tests need no database."""
    with patch(
        "sessions.merge_request_runs.tasks.aget_task_run", AsyncMock(return_value=TaskRun("run-1", 5))
    ) as lookup:
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


def _mention_task(**kwargs):
    return address_merge_request_task.func(
        _CONTEXT,
        **{
            "repo_id": "group/repo",
            "merge_request_id": 7,
            "request_key": "note:5",
            "mention_comment_id": "d1",
            "thread_id": "t-7",
            "use_max": False,
        }
        | kwargs,
    )


async def test_address_mr_comments_skips_when_merged(run_lookup):
    client = MagicMock()
    client.get_merge_request.return_value = _mr(merged=True)

    with (
        patch("sessions.merge_request_runs.tasks.RepoClient.create_instance", return_value=client),
        patch(_ADDRESS) as address,
    ):
        result = await _mention_task()

    address.assert_not_called()
    run_lookup.assert_not_awaited()
    client.create_merge_request_comment.assert_called_once()
    assert "already been merged" in result["response"]
    assert result["code_changes"] is False


async def test_address_mr_comments_skips_when_branch_gone():
    client = MagicMock()
    client.get_merge_request.return_value = _mr(merged=False)

    with (
        patch("sessions.merge_request_runs.tasks.RepoClient.create_instance", return_value=client),
        patch(_ADDRESS, AsyncMock(side_effect=CloneRefNotFoundError("chore/x", "group/repo"))),
    ):
        result = await _mention_task()

    client.create_merge_request_comment.assert_called_once()
    assert "no longer exists" in result["response"]
    assert result["code_changes"] is False


async def test_address_mr_comments_hands_the_merge_request_and_its_run_to_the_addressor(run_lookup):
    client = MagicMock()
    merge_request = _mr(merged=False)
    client.get_merge_request.return_value = merge_request
    address = AsyncMock(return_value={"response": "done"})

    with (
        patch("sessions.merge_request_runs.tasks.RepoClient.create_instance", return_value=client),
        patch(_ADDRESS, address),
    ):
        result = await _mention_task(mention_note_id=5, sandbox_environment_id="e")

    assert result == {"response": "done"}
    assert address.await_args.kwargs == {
        "repo_id": "group/repo",
        "merge_request": merge_request,
        "mention_comment_id": "d1",
        "mention_note_id": 5,
        "thread_id": "t-7",
        "sandbox_env_id": "e",
        "run_id": "run-1",
        "use_max": False,
        "acting_user_id": 5,
    }
    run_lookup.assert_awaited_once_with("tr-1")


def _job_task(**kwargs):
    return _mention_task(
        **{"request_key": "job:abc", "mention_comment_id": None, "prompt": "/code-review", "requester_username": "ci"}
        | kwargs
    )


@pytest.mark.django_db(transaction=True)
class TestAJobThatCannotStart:
    @pytest.fixture
    async def run(self, run_lookup):
        session = await Session.objects.acreate(thread_id="t-7", origin=SessionOrigin.API_JOB, repo_id="group/repo")
        run = await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.API_JOB, repo_id="group/repo", status=RunStatus.RUNNING
        )
        run_lookup.return_value = TaskRun(str(run.pk), None)
        return run

    @pytest.mark.parametrize(
        ("state", "note"),
        [
            ({"merged": True}, "This merge request was merged before the job could start, so I didn't run it."),
            ({"closed": True}, "This merge request was closed before the job could start, so I didn't run it."),
        ],
    )
    async def test_a_merge_request_no_longer_open_is_told_and_the_job_fails(self, run, state, note):
        client = MagicMock()
        client.get_merge_request.return_value = _mr(merged=False).model_copy(update=state)

        with (
            patch("sessions.merge_request_runs.tasks.RepoClient.create_instance", return_value=client),
            patch(_ADDRESS) as address,
        ):
            result = await _job_task()

        address.assert_not_called()
        client.create_merge_request_comment.assert_called_once_with("group/repo", 7, body=note)
        await run.arefresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.FAILED, note)
        assert result["response"] == note

    async def test_an_archived_session_fails_the_job_and_posts_nothing(self, run):
        await Session.objects.filter(pk="t-7").aupdate(archived_at=timezone.now())
        client = MagicMock()
        client.get_merge_request.return_value = _mr(merged=False)

        with (
            patch("sessions.merge_request_runs.tasks.RepoClient.create_instance", return_value=client),
            patch(_ADDRESS) as address,
        ):
            await _job_task()

        address.assert_not_called()
        client.create_merge_request_comment.assert_not_called()
        await run.arefresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.FAILED, NOT_RUN_ARCHIVED)

    async def test_a_mention_on_a_merged_merge_request_still_gets_todays_note(self, run):
        client = MagicMock()
        client.get_merge_request.return_value = _mr(merged=True)

        with patch("sessions.merge_request_runs.tasks.RepoClient.create_instance", return_value=client):
            result = await _mention_task()

        assert "already been merged" in result["response"]
        await run.arefresh_from_db()
        assert run.status == RunStatus.RUNNING


async def test_a_job_hands_its_prompt_and_model_to_the_addressor():
    client = MagicMock()
    client.get_merge_request.return_value = _mr(merged=False)
    address = AsyncMock(return_value={"response": "done"})

    with (
        patch("sessions.merge_request_runs.tasks.RepoClient.create_instance", return_value=client),
        patch("sessions.merge_request_runs.tasks._astop_job", AsyncMock(return_value=None)),
        patch(_ADDRESS, address),
    ):
        await _job_task(agent_model="openrouter:x", agent_thinking_level="low")

    assert (
        address.await_args.kwargs
        | {
            "prompt": "/code-review",
            "requester_username": "ci",
            "agent_model": "openrouter:x",
            "agent_thinking_level": "low",
            "mention_comment_id": None,
        }
        == address.await_args.kwargs
    )
