from unittest.mock import AsyncMock, Mock, patch

from django.core.cache import cache
from django.utils import timezone

import pytest
from django_tasks_db.models import DBTaskResult
from gitlab.exceptions import GitlabGetError
from sessions.ids import compute_thread_id
from sessions.inbox.drain import Drain
from sessions.inbox.merge_request_jobs import NOT_STARTED_MERGED
from sessions.merge_request_runs.service import MergeRequestRuns
from sessions.merge_request_runs.starter import Mention, Request, Requester
from sessions.models import EventKind, InboxEvent, MergeRequestOwnership, Run, RunStatus, Session, SessionOrigin
from sessions.services import BatchSubmitFailure

from codebase.base import GitPlatform, MergeRequest, Scope
from codebase.base import User as PlatformUser
from tests.unit_tests.sessions.conftest import amake_owned_merge_request

pytestmark = pytest.mark.django_db(transaction=True)

THREAD = compute_thread_id(repo_slug="group/repo", scope=Scope.MERGE_REQUEST, entity_iid=7)
CI_ACTOR = Requester(user=None, provider=GitPlatform.GITLAB, uid=9, username="pusher", acts_as_user=False)


def _request(**fields) -> Request:
    return Request(**{"prompt": "/code-review", "source": SessionOrigin.API_JOB, "request_key": "job:abc"} | fields)


def _merge_request(**fields) -> MergeRequest:
    return MergeRequest(
        **{
            "repo_id": "group/repo",
            "merge_request_id": 7,
            "source_branch": "feat",
            "target_branch": "main",
            "title": "Feature",
            "description": "",
            "author": PlatformUser(id=1, username="author"),
        }
        | fields
    )


@pytest.fixture
def client():
    client = Mock()
    client.git_platform = GitPlatform.GITLAB
    client.get_merge_request.return_value = _merge_request()
    return client


@pytest.fixture(autouse=True)
def no_drain():
    with patch.object(Drain, "start_logged"):
        yield


@pytest.fixture
def no_env():
    with patch("sessions.merge_request_runs.starter.resolve_env_for_run", new=AsyncMock(return_value=None)):
        yield


@pytest.mark.usefixtures("database_task_backend", "no_env")
class TestAJobOnAMergeRequestNoSessionOwns:
    async def test_it_runs_in_the_merge_requests_session_as_the_job_it_is(self, client):
        outcome = await MergeRequestRuns(client, "group/repo", 7).astart_job(
            CI_ACTOR, _request(source=SessionOrigin.MCP_JOB, muted=True), title="Feature", ref="feat"
        )

        run = await Run.objects.aget(pk=outcome.run.pk)
        assert (outcome.started, run.status, outcome.thread_id) == (True, RunStatus.READY, THREAD)
        assert (run.session_id, run.trigger_type, run.merge_request_iid, run.ref, run.prompt) == (
            THREAD, SessionOrigin.MCP_JOB, 7, "feat", "/code-review",
        )  # fmt: skip
        assert (run.user_id, run.external_provider, run.external_uid, run.external_username) == (
            None, "gitlab", "9", "pusher",
        )  # fmt: skip
        assert (run.acts_as_user, run.muted) == (False, True)
        task = await DBTaskResult.objects.aget(pk=run.task_result_id)
        assert (
            task.args_kwargs["kwargs"] | {"request_key": "job:abc", "prompt": "/code-review"}
            == (task.args_kwargs["kwargs"])
        )
        assert task.args_kwargs["kwargs"]["requester_username"] == "pusher"

    @pytest.mark.parametrize(("username", "expected"), [("", ""), ("jdoe", "jdoe")], ids=["unlinked", "linked"])
    async def test_a_caller_owns_the_run_and_is_named_only_by_their_platform_account(
        self, client, member_user, username, expected
    ):
        outcome = await MergeRequestRuns(client, "group/repo", 7).astart_job(
            Requester(user=member_user, username=username), _request(), title="Feature", ref="feat"
        )

        run = await Run.objects.aget(pk=outcome.run.pk)
        assert (run.user_id, run.acts_as_user, run.trigger_type) == (member_user.pk, True, SessionOrigin.API_JOB)
        task = await DBTaskResult.objects.aget(pk=run.task_result_id)
        assert task.args_kwargs["kwargs"]["requester_username"] == expected

    async def test_its_run_runs_alone_with_the_jobs_references_and_the_overrides_admission_resolved(self, client):
        request = _request(
            references=({"key": "PROJ-1"},), mcp_switches={"grafana": True}, mcp_overrides={"grafana": "on"}
        )

        outcome = await MergeRequestRuns(client, "group/repo", 7).astart_job(CI_ACTOR, request, title="F", ref="feat")

        run = await Run.objects.aget(pk=outcome.run.pk)
        assert run.runs_alone is True
        kwargs = (await DBTaskResult.objects.aget(pk=run.task_result_id)).args_kwargs["kwargs"]
        assert (kwargs["references"], kwargs["mcp_overrides"]) == ([{"key": "PROJ-1"}], {"grafana": "on"})

    async def test_a_repeated_request_key_starts_one_run(self, client):
        runs = MergeRequestRuns(client, "group/repo", 7)

        first = await runs.astart_job(CI_ACTOR, _request(), title="Feature", ref="feat")
        second = await runs.astart_job(CI_ACTOR, _request(), title="Feature", ref="feat")

        assert (first.started, second.started) == (True, False)
        assert await Run.objects.acount() == 1

    async def test_two_jobs_on_one_merge_request_both_run(self, client):
        runs = MergeRequestRuns(client, "group/repo", 7)

        await runs.astart_job(CI_ACTOR, _request(request_key="job:1"), title="Feature", ref="feat")
        await runs.astart_job(CI_ACTOR, _request(request_key="job:2"), title="Feature", ref="feat")

        assert await Run.objects.filter(session_id=THREAD, status=RunStatus.READY).acount() == 2

    async def test_an_archived_session_starts_nothing_and_posts_nothing(self, client):
        await Session.objects.acreate(
            thread_id=THREAD, origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", archived_at=timezone.now()
        )

        outcome = await MergeRequestRuns(client, "group/repo", 7).astart_job(
            CI_ACTOR, _request(), title="F", ref="feat"
        )

        assert (outcome.archived, outcome.thread_id, outcome.run) == (True, THREAD, None)
        assert not await Run.objects.aexists()
        client.create_merge_request_comment.assert_not_called()

    async def test_an_archived_session_still_answers_a_mention(self, client):
        await cache.aclear()
        await Session.objects.acreate(
            thread_id=THREAD, origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", archived_at=timezone.now()
        )

        outcome = await MergeRequestRuns(client, "group/repo", 7).astart(
            CI_ACTOR, Mention(note_id=3, comment_id="d-3"), title="F", ref="feat"
        )

        assert outcome.archived is True
        assert "This session is archived" in client.create_merge_request_comment.call_args.args[2]


class TestAJobOnAnOwnedMergeRequest:
    @pytest.fixture
    async def owned(self):
        return await amake_owned_merge_request(repo_id="group/repo", merge_request_iid=7)

    async def test_it_posts_a_job_with_a_run_of_its_own(self, client, owned):
        outcome = await MergeRequestRuns(client, "group/repo", 7).astart_job(
            CI_ACTOR, _request(source=SessionOrigin.MCP_JOB, muted=True), title="F", ref=owned.source_branch
        )

        event = await InboxEvent.objects.aget(session_id=owned.session_id)
        run = await Run.objects.aget(pk=outcome.run.pk)
        assert (outcome.thread_id, outcome.started, run.status) == (owned.session_id, True, RunStatus.QUEUED)
        assert (event.kind, event.wake, event.subject, event.source_key, event.run_id) == (
            EventKind.MERGE_REQUEST_JOB, True, "", "job:abc", run.pk,
        )  # fmt: skip
        assert event.payload == {"prompt": "/code-review", "trigger_type": SessionOrigin.MCP_JOB, "muted": True}
        assert (event.author_provider, event.author_uid, event.author_username) == ("gitlab", "9", "pusher")
        assert (run.trigger_type, run.runs_alone, run.acts_as_user, run.merge_request_iid, run.ref) == (
            SessionOrigin.MCP_JOB, True, False, 7, owned.source_branch,
        )  # fmt: skip

    async def test_its_options_ride_on_its_event_for_its_run(self, client, owned, member_user):
        request = _request(
            references=({"key": "PROJ-1"},),
            mcp_switches={"grafana": True},
            mcp_overrides={"grafana": "on"},
            sandbox_environment_id="env-1",
            environment_named=True,
        )

        outcome = await MergeRequestRuns(client, "group/repo", 7).astart_job(
            Requester(user=member_user), request, title="F", ref=owned.source_branch
        )

        payload = (await InboxEvent.objects.aget(run=outcome.run)).payload
        assert (payload["references"], payload["mcp_switches"]) == ([{"key": "PROJ-1"}], {"grafana": True})
        assert payload["sandbox_environment_id"] == "env-1"
        assert "mcp_overrides" not in payload

    async def test_an_archived_owner_posts_nothing(self, client, owned):
        await Session.objects.filter(pk=owned.session_id).aupdate(archived_at=timezone.now())

        outcome = await MergeRequestRuns(client, "group/repo", 7).astart_job(CI_ACTOR, _request(), title="F", ref="")

        assert (outcome.archived, outcome.thread_id) == (True, owned.session_id)
        assert not await InboxEvent.objects.aexists()
        client.create_merge_request_comment.assert_not_called()


@pytest.mark.usefixtures("database_task_backend", "no_env")
class TestSubmittingAJob:
    async def test_an_open_merge_request_starts_the_job(self, client):
        outcome = await MergeRequestRuns(client, "group/repo", 7).asubmit_job(CI_ACTOR, _request())

        assert isinstance(outcome, Run)
        assert (outcome.ref, outcome.title) == ("feat", "Feature")

    @pytest.mark.parametrize(
        ("merge_request", "error"),
        [({"merged": True}, "Merge request !7 is merged."), ({"closed": True}, "Merge request !7 is closed.")],
    )
    async def test_a_merge_request_that_is_not_open_is_refused(self, client, merge_request, error):
        client.get_merge_request.return_value = _merge_request(**merge_request)

        outcome = await MergeRequestRuns(client, "group/repo", 7).asubmit_job(CI_ACTOR, _request())

        assert outcome == BatchSubmitFailure(repo_id="group/repo", ref="", error=error)
        assert not await Run.objects.aexists()

    async def test_a_missing_merge_request_is_refused(self, client):
        client.get_merge_request.side_effect = GitlabGetError("404 Not Found", response_code=404)

        outcome = await MergeRequestRuns(client, "group/repo", 7).asubmit_job(CI_ACTOR, _request())

        assert outcome.error == "Merge request !7 was not found in group/repo."

    async def test_an_unreadable_merge_request_is_refused_and_logged(self, client, caplog):
        client.get_merge_request.side_effect = GitlabGetError("500", response_code=500)

        outcome = await MergeRequestRuns(client, "group/repo", 7).asubmit_job(CI_ACTOR, _request())

        assert outcome.error == "Merge request !7 of group/repo could not be read; try again."
        assert any(record.levelname == "ERROR" for record in caplog.records)

    async def test_an_archived_session_is_refused_with_its_link(self, client):
        await Session.objects.acreate(
            thread_id=THREAD, origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", archived_at=timezone.now()
        )

        outcome = await MergeRequestRuns(client, "group/repo", 7).asubmit_job(CI_ACTOR, _request())

        assert outcome.error.startswith(
            "The DAIV session of merge request !7 is archived. Unarchive it to run jobs on it: "
        )
        assert outcome.error.endswith(f"/sessions/{THREAD}/")
        client.create_merge_request_comment.assert_not_called()

    @pytest.mark.parametrize(
        ("error_message", "error"),
        [
            ("access_denied: internal detail", "The job on merge request !7 could not start."),
            (NOT_STARTED_MERGED, NOT_STARTED_MERGED),
        ],
        ids=["generic", "not-started"],
    )
    async def test_a_job_whose_run_failed_before_it_started_is_refused(self, client, error_message, error):
        owned = await amake_owned_merge_request(repo_id="group/repo", merge_request_iid=7)

        async def fail(_self, *args, **kwargs):
            return await Run.objects.acreate(
                session_id=owned.session_id, trigger_type=SessionOrigin.API_JOB, repo_id="group/repo",
                status=RunStatus.FAILED, error_message=error_message,
            )  # fmt: skip

        with patch("sessions.merge_request_runs.service.MergeRequestJobs.apost", new=fail):
            outcome = await MergeRequestRuns(client, "group/repo", 7).asubmit_job(CI_ACTOR, _request())

        assert outcome == BatchSubmitFailure(repo_id="group/repo", ref="", error=error)

    async def test_a_merge_request_that_closed_before_the_post_is_refused(self, client):
        owned = await amake_owned_merge_request(repo_id="group/repo", merge_request_iid=7)
        await MergeRequestOwnership.objects.filter(pk=owned.pk).aupdate(closed_at=timezone.now())

        with patch("sessions.merge_request_runs.service.Ownerships.afind_open", new=AsyncMock(return_value=owned)):
            outcome = await MergeRequestRuns(client, "group/repo", 7).asubmit_job(CI_ACTOR, _request())

        assert outcome.error == "Merge request !7 is closed."
        assert not await InboxEvent.objects.aexists()
