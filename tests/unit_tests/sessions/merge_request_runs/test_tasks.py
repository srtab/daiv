from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.utils import timezone

import pytest
from langchain_core.messages import AIMessage
from sessions.executor.lock import SessionLock
from sessions.inbox.drain import Drain
from sessions.legs import NOT_RUN_ARCHIVED
from sessions.merge_request_runs.tasks import address_merge_request_task
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.services import TaskRun

from codebase.base import MergeRequest, User
from codebase.exceptions import CloneRefNotFoundError
from tests.unit_tests.sessions.merge_request_runs.conftest import addressor_agent, addressor_ctx, addressor_run

_CONTEXT = SimpleNamespace(task_result=SimpleNamespace(id="tr-1"))
_ADDRESS = "sessions.merge_request_runs.addressor.CommentsAddressorManager.address_comments"


@pytest.fixture(autouse=True)
def run_lookup():
    """The Run linked to ``_CONTEXT``'s task result, stubbed so these tests need no database."""
    lookup = AsyncMock(return_value=TaskRun("run-1", 5))
    with (
        patch("sessions.merge_request_runs.tasks.aget_task_run", lookup),
        patch("sessions.merge_request_runs.guards.aget_task_run", lookup),
    ):
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
        "prompt": None,
        "requester_username": "",
        "agent_model": None,
        "agent_thinking_level": None,
        "references": None,
        "mcp_overrides": None,
        "on_context_ready": None,
    }
    run_lookup.assert_awaited_once_with("tr-1")


def _job_task(**kwargs):
    return _mention_task(
        **{"request_key": "job:abc", "mention_comment_id": None, "prompt": "/code-review", "requester_username": "ci"}
        | kwargs
    )


async def test_a_job_its_guard_ends_returns_the_guards_result_without_the_addressor():
    client = MagicMock()
    client.get_merge_request.return_value = _mr(merged=True)
    ended = {"response": "not run"}

    with (
        patch("sessions.merge_request_runs.tasks.RepoClient.create_instance", return_value=client),
        patch("sessions.merge_request_runs.guards.JobGuard.askip", AsyncMock(return_value=ended)),
        patch(_ADDRESS) as address,
    ):
        result = await _job_task()

    assert result is ended
    address.assert_not_called()


async def test_a_job_whose_source_branch_is_gone_ends_through_its_guard():
    client = MagicMock()
    client.get_merge_request.return_value = _mr(merged=False)
    ended = {"response": "branch gone"}

    with (
        patch("sessions.merge_request_runs.tasks.RepoClient.create_instance", return_value=client),
        patch("sessions.merge_request_runs.guards.JobGuard.askip", AsyncMock(return_value=None)),
        patch("sessions.merge_request_runs.guards.JobGuard.abranch_gone", AsyncMock(return_value=ended)),
        patch(_ADDRESS, AsyncMock(side_effect=CloneRefNotFoundError("chore/x", "group/repo"))),
    ):
        result = await _job_task()

    assert result is ended


@pytest.mark.django_db(transaction=True)
async def test_a_mention_on_a_merged_merge_request_leaves_its_run_alone(run_lookup):
    session = await Session.objects.acreate(thread_id="t-7", origin=SessionOrigin.API_JOB, repo_id="group/repo")
    run = await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.API_JOB, repo_id="group/repo", status=RunStatus.RUNNING
    )
    run_lookup.return_value = TaskRun(str(run.pk), None)
    client = MagicMock()
    client.get_merge_request.return_value = _mr(merged=True)

    with patch("sessions.merge_request_runs.tasks.RepoClient.create_instance", return_value=client):
        result = await _mention_task()

    assert "already been merged" in result["response"]
    await run.arefresh_from_db()
    assert run.status == RunStatus.RUNNING


@pytest.mark.django_db(transaction=True)
class TestAJobThatWaitedForTheSessionSlot:
    """Another run held the merge request's session when the job's task started; the job checks again once it holds
    the slot."""

    @pytest.fixture
    async def run(self, run_lookup):
        session = await Session.objects.acreate(
            thread_id="t-7", origin=SessionOrigin.API_JOB, repo_id="group/repo", active_run_id="first-job"
        )
        run = await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.API_JOB, repo_id="group/repo", status=RunStatus.RUNNING
        )
        run_lookup.return_value = TaskRun(str(run.pk), None)
        return run

    def _while_waiting(self, meanwhile):
        """``SessionLock.try_claim`` that, when the slot is taken, runs ``meanwhile`` and frees the slot."""
        real_try_claim = SessionLock.try_claim

        async def _try_claim(thread_id, holder_id):
            claimed = await real_try_claim(thread_id, holder_id)
            if not claimed:
                await meanwhile()
                await SessionLock.release(thread_id, "first-job")
            return claimed

        return patch("sessions.executor.lock.SessionLock.try_claim", _try_claim)

    async def _run_job(self, client, meanwhile):
        agent = addressor_agent(return_value={"messages": [AIMessage(content="done")]})
        with (
            patch("sessions.merge_request_runs.tasks.RepoClient.create_instance", return_value=client),
            addressor_run(agent, real_lock=True, ctx=addressor_ctx()) as stack,
            patch("sessions.executor.lock.LOCK_POLL_INTERVAL_S", 0.01),
            self._while_waiting(meanwhile),
        ):
            result = await _job_task()
        return result, stack

    async def test_a_merge_request_merged_meanwhile_is_told_and_the_job_fails_without_starting(
        self, run, captured_client
    ):
        note = "This merge request was merged before the job could start, so I didn't run it."
        captured_client.get_merge_request.return_value = _mr(merged=False)

        async def merge():
            captured_client.get_merge_request.return_value = _mr(merged=True)

        result, stack = await self._run_job(captured_client, merge)

        stack.create_agent.assert_not_awaited()
        captured_client.create_merge_request_comment.assert_called_once_with("group/repo", 7, body=note)
        await run.arefresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.FAILED, note)
        assert result["response"] == note

    async def test_a_job_stopped_once_it_held_the_slot_drains_its_session_after_freeing_it(self, run, captured_client):
        captured_client.get_merge_request.return_value = _mr(merged=False)

        async def merge():
            captured_client.get_merge_request.return_value = _mr(merged=True)

        slots = []

        def start_logged(drain):
            slots.append((drain.thread_id, Session.objects.get(pk=drain.thread_id).active_run_id))

        with patch.object(Drain, "start_logged", autospec=True, side_effect=start_logged):
            await self._run_job(captured_client, merge)

        assert slots[-1] == ("t-7", None)

    async def test_a_session_archived_meanwhile_fails_the_job_and_posts_nothing(self, run, captured_client):
        captured_client.get_merge_request.return_value = _mr(merged=False)

        async def archive():
            await Session.objects.filter(pk="t-7").aupdate(archived_at=timezone.now())

        _result, stack = await self._run_job(captured_client, archive)

        stack.create_agent.assert_not_awaited()
        captured_client.create_merge_request_comment.assert_not_called()
        await run.arefresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.FAILED, NOT_RUN_ARCHIVED)

    async def test_a_merge_request_still_open_runs_the_job(self, run, captured_client):
        captured_client.get_merge_request.return_value = _mr(merged=False)

        result, stack = await self._run_job(captured_client, AsyncMock())

        stack.create_agent.assert_awaited_once()
        captured_client.create_merge_request_comment.assert_called_once()
        assert captured_client.create_merge_request_comment.call_args.args[2] == "done"
        await run.arefresh_from_db()
        assert run.status == RunStatus.RUNNING
        assert result["response"] == "done"


async def test_a_job_hands_its_prompt_and_model_to_the_addressor():
    client = MagicMock()
    client.get_merge_request.return_value = _mr(merged=False)
    address = AsyncMock(return_value={"response": "done"})

    with (
        patch("sessions.merge_request_runs.tasks.RepoClient.create_instance", return_value=client),
        patch("sessions.merge_request_runs.guards.JobGuard.askip", AsyncMock(return_value=None)),
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
