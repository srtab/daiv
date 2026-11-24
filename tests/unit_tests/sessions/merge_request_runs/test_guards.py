import logging
from unittest.mock import AsyncMock, MagicMock, patch

from django.utils import timezone

import pytest
from github import GithubException
from gitlab.exceptions import GitlabGetError
from sessions.inbox.merge_request_jobs import NOT_STARTED_BRANCH_GONE, NOT_STARTED_CLOSED, NOT_STARTED_MERGED
from sessions.legs import NOT_RUN_ARCHIVED
from sessions.merge_request_runs.guards import JobGuard, JobNotStarted, MentionGuard
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.services import TaskRun

from codebase.base import MergeRequest, User

_PLATFORM_ERRORS = [
    GitlabGetError("502 Bad Gateway", response_code=502),
    GithubException(502, {}, {}),
    ConnectionError(),
]


def _mr(**state) -> MergeRequest:
    return MergeRequest(
        repo_id="group/repo",
        merge_request_id=7,
        source_branch="chore/x",
        target_branch="dev",
        title="t",
        description="d",
        web_url="https://git/group/repo/-/merge_requests/7",
        author=User(id=1, username="u", name="U"),
        **state,
    )


class TestMentionGuard:
    async def test_a_mention_on_an_open_merge_request_goes_ahead(self):
        client = MagicMock()

        assert await MentionGuard(client, _mr()).askip() is None
        client.create_merge_request_comment.assert_not_called()

    async def test_a_mention_on_a_merged_merge_request_is_answered_there(self):
        client = MagicMock()

        result = await MentionGuard(client, _mr(merged=True)).askip()

        body = client.create_merge_request_comment.call_args.kwargs["body"]
        client.create_merge_request_comment.assert_called_once_with("group/repo", 7, body=body)
        assert "already been merged" in body
        assert "`chore/x`" in body
        assert (result["response"], result["code_changes"], result["merge_request_id"]) == (body, False, 7)
        assert result["merge_request_web_url"] == "https://git/group/repo/-/merge_requests/7"

    async def test_a_mention_whose_source_branch_is_gone_is_answered_there(self):
        client = MagicMock()

        result = await MentionGuard(client, _mr()).abranch_gone()

        body = client.create_merge_request_comment.call_args.kwargs["body"]
        assert "`chore/x`" in body
        assert "no longer exists" in body
        assert result["response"] == body


@pytest.mark.django_db(transaction=True)
class TestJobGuard:
    @pytest.fixture
    def task_run(self):
        lookup = AsyncMock(return_value=TaskRun())
        with patch("sessions.merge_request_runs.guards.aget_task_run", lookup):
            yield lookup

    @pytest.fixture
    async def run(self, task_run):
        session = await Session.objects.acreate(thread_id="t-7", origin=SessionOrigin.API_JOB, repo_id="group/repo")
        run = await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.API_JOB, repo_id="group/repo", status=RunStatus.RUNNING
        )
        task_run.return_value = TaskRun(str(run.pk), None)
        return run

    @staticmethod
    def _guard(client, merge_request: MergeRequest) -> JobGuard:
        return JobGuard(client, merge_request, thread_id="t-7", task_result_id="tr-1")

    @pytest.mark.parametrize("state", [{}, {"draft": True}], ids=["open", "draft"])
    async def test_an_open_merge_request_lets_the_job_start(self, run, state):
        client = MagicMock()

        assert await self._guard(client, _mr(**state)).askip() is None

        client.create_merge_request_comment.assert_not_called()
        assert (await Run.objects.aget(pk=run.pk)).status == RunStatus.RUNNING

    @pytest.mark.parametrize(
        ("state", "note"), [({"merged": True}, NOT_STARTED_MERGED), ({"closed": True}, NOT_STARTED_CLOSED)]
    )
    async def test_a_merge_request_no_longer_open_ends_the_job_saying_why(self, run, state, note):
        client = MagicMock()

        result = await self._guard(client, _mr(**state)).askip()

        assert result["response"] == note
        client.create_merge_request_comment.assert_called_once_with("group/repo", 7, body=note)
        stopped = await Run.objects.aget(pk=run.pk)
        assert (stopped.status, stopped.error_message) == (RunStatus.FAILED, note)

    async def test_an_archived_session_ends_the_job_and_posts_nothing(self, run):
        await Session.objects.filter(pk="t-7").aupdate(archived_at=timezone.now())
        client = MagicMock()

        result = await self._guard(client, _mr()).askip()

        assert result["response"] == ""
        client.create_merge_request_comment.assert_not_called()
        stopped = await Run.objects.aget(pk=run.pk)
        assert (stopped.status, stopped.error_message) == (RunStatus.FAILED, NOT_RUN_ARCHIVED)

    async def test_the_recheck_reads_the_merge_request_again_and_ends_a_job_it_finds_merged(self, run):
        client = MagicMock()
        client.get_merge_request.return_value = _mr(merged=True)
        guard = self._guard(client, _mr())

        with pytest.raises(JobNotStarted) as stopped:
            await guard.arecheck("chore/x")

        client.get_merge_request.assert_called_once_with("group/repo", 7)
        assert stopped.value.result["response"] == NOT_STARTED_MERGED
        assert guard.merge_request.merged
        assert (await Run.objects.aget(pk=run.pk)).error_message == NOT_STARTED_MERGED

    async def test_the_recheck_lets_a_merge_request_still_open_go_ahead(self, run):
        client = MagicMock()
        client.get_merge_request.return_value = _mr()

        assert await self._guard(client, _mr()).arecheck("chore/x") is None

        client.create_merge_request_comment.assert_not_called()
        assert (await Run.objects.aget(pk=run.pk)).status == RunStatus.RUNNING

    @pytest.mark.parametrize("error", _PLATFORM_ERRORS, ids=["gitlab", "github", "network"])
    async def test_a_platform_error_on_the_recheck_lets_the_job_go_ahead_on_what_it_read(self, run, caplog, error):
        client = MagicMock()
        client.get_merge_request.side_effect = error

        assert await self._guard(client, _mr()).arecheck("chore/x") is None

        client.create_merge_request_comment.assert_not_called()
        assert (await Run.objects.aget(pk=run.pk)).status == RunStatus.RUNNING
        assert [record.levelno for record in caplog.records if record.name == "daiv.tasks"] == [logging.WARNING]

    async def test_a_platform_error_on_the_recheck_still_ends_the_job_of_an_archived_session(self, run):
        await Session.objects.filter(pk="t-7").aupdate(archived_at=timezone.now())
        client = MagicMock()
        client.get_merge_request.side_effect = _PLATFORM_ERRORS[0]

        with pytest.raises(JobNotStarted):
            await self._guard(client, _mr()).arecheck("chore/x")

        assert (await Run.objects.aget(pk=run.pk)).error_message == NOT_RUN_ARCHIVED

    @pytest.mark.parametrize("error", _PLATFORM_ERRORS, ids=["gitlab", "github", "network"])
    async def test_a_platform_error_reading_it_again_says_the_source_branch_is_gone(self, run, error):
        client = MagicMock()
        client.get_merge_request.side_effect = error

        result = await self._guard(client, _mr()).abranch_gone()

        client.create_merge_request_comment.assert_called_once_with("group/repo", 7, body=NOT_STARTED_BRANCH_GONE)
        assert result["response"] == NOT_STARTED_BRANCH_GONE
        stopped = await Run.objects.aget(pk=run.pk)
        assert (stopped.status, stopped.error_message) == (RunStatus.FAILED, NOT_STARTED_BRANCH_GONE)

    @pytest.mark.parametrize(
        ("merged_when_read_again", "note"),
        [
            pytest.param(False, NOT_STARTED_BRANCH_GONE, id="deleted"),
            pytest.param(True, NOT_STARTED_MERGED, id="merged-and-deleted"),
        ],
    )
    async def test_a_gone_source_branch_ends_the_job_saying_why(self, run, merged_when_read_again, note):
        client = MagicMock()
        client.get_merge_request.return_value = _mr(merged=merged_when_read_again)

        result = await self._guard(client, _mr()).abranch_gone()

        client.get_merge_request.assert_called_once_with("group/repo", 7)
        client.create_merge_request_comment.assert_called_once_with("group/repo", 7, body=note)
        assert result["response"] == note
        stopped = await Run.objects.aget(pk=run.pk)
        assert (stopped.status, stopped.error_message) == (RunStatus.FAILED, note)

    async def test_a_job_with_no_linked_run_is_still_told_on_the_merge_request(self, task_run):
        client = MagicMock()

        result = await self._guard(client, _mr(merged=True)).askip()

        task_run.assert_awaited_once_with("tr-1")
        client.create_merge_request_comment.assert_called_once_with("group/repo", 7, body=NOT_STARTED_MERGED)
        assert result["response"] == NOT_STARTED_MERGED
        assert not await Run.objects.aexists()
