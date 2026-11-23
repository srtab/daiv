import logging
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from gitlab.exceptions import GitlabGetError
from jobs.api.ci import ONE_JOB_ON_ITS_REPOSITORY, UNAUTHORIZED, CISubmission
from jobs.api.schemas import JobSubmitRequest
from sessions.admission import AdmissionError
from sessions.merge_request_runs.starter import Caller
from sessions.models import MergeRequestOwnership, Run, RunStatus, Session, SessionOrigin
from sessions.services import BatchSubmitFailure, BatchSubmitResult, RepoTarget

from accounts.api.ci_identity import CIIdentity, CIIdentityTokens
from accounts.models import User
from codebase.base import GitPlatform
from codebase.repo_config import RepositoryConfig


def _identity(**fields) -> CIIdentity:
    """A verified token of ``group/project`` (platform id 1, the mocked repository's) triggered by GitLab user 9."""
    return CIIdentity(
        **{
            "provider": GitPlatform.GITLAB,
            "repo_id": "group/project",
            "repo_platform_id": 1,
            "actor_uid": "9",
            "actor_username": "pusher",
            "user": None,
            "sha": None,
            "jti": "jti-1",
            "expires_at": timezone.now() + timedelta(minutes=5),
        }
        | fields
    )


def _payload(**fields) -> JobSubmitRequest:
    return JobSubmitRequest(**({"repos": [{"repo_id": "group/project"}], "prompt": "/code-review"} | fields))


class TestRefusal:
    @pytest.fixture(autouse=True)
    def repo_config(self):
        with patch("jobs.api.ci.RepositoryConfig.get_config", return_value=RepositoryConfig()) as get_config:
            yield get_config

    async def test_a_job_on_its_own_repository_is_admitted_and_claims_the_token(self, mock_repo_client):
        identity = _identity()

        assert await CISubmission(identity).arefusal(_payload()) is None
        assert await CIIdentityTokens.aclaim(identity) is False

    async def test_anything_but_one_job_on_its_repository_is_refused_before_the_repository_is_read(
        self, mock_repo_client
    ):
        refusal = await CISubmission(_identity()).arefusal(_payload(shared_session=True))

        assert refusal == (400, {"detail": ONE_JOB_ON_ITS_REPOSITORY})
        mock_repo_client.get_repository.assert_not_called()

    async def test_a_platform_outage_is_unauthorized_and_logged_once_at_error(self, mock_repo_client, caplog):
        identity = _identity()
        mock_repo_client.get_repository.side_effect = GitlabGetError("502 Bad Gateway", response_code=502)

        with caplog.at_level(logging.WARNING):
            refusal = await CISubmission(identity).arefusal(_payload())

        assert refusal == (401, UNAUTHORIZED)
        assert [record.levelno for record in caplog.records] == [logging.ERROR]
        assert await CIIdentityTokens.aclaim(identity) is True

    async def test_an_actor_outside_the_allowlist_does_not_use_the_token(self, mock_repo_client, repo_config):
        identity = _identity()
        repo_config.return_value = RepositoryConfig(allowed_usernames=["alice"])

        assert await CISubmission(identity).arefusal(_payload()) == (401, UNAUTHORIZED)
        assert await CIIdentityTokens.aclaim(identity) is True


class TestSubmit:
    @pytest.fixture
    def submit(self):
        with patch("jobs.api.ci.asubmit_ci_job", new=AsyncMock()) as submit:
            yield submit

    @pytest.fixture(autouse=True)
    def release(self):
        with patch.object(CIIdentityTokens, "arelease", new=AsyncMock()) as release:
            yield release

    def _target(self):
        return RepoTarget(repo_id="group/project", ref="", prompt="")

    async def test_an_accepted_job_runs_as_no_one_and_keeps_the_token_used(self, submit, release):
        identity = _identity()
        submit.return_value = BatchSubmitResult(batch_id=uuid.uuid4(), outcomes=[SimpleNamespace()])

        await CISubmission(identity).asubmit(self._target(), _payload(), prompt="/code-review")

        submit.assert_awaited_once_with(
            caller=Caller(user=None, provider=GitPlatform.GITLAB, uid="9", username="pusher", acts_as_user=False),
            target=self._target(),
            muted=True,
            prompt="/code-review",
        )
        release.assert_not_awaited()

    async def test_a_submission_that_accepted_no_job_releases_the_token(self, submit, release):
        identity = _identity()
        failure = BatchSubmitFailure(repo_id="group/project", ref="", error="Merge request !1 is merged.")
        submit.return_value = BatchSubmitResult(batch_id=uuid.uuid4(), outcomes=[failure])

        await CISubmission(identity).asubmit(self._target(), _payload(), prompt="/code-review")

        release.assert_awaited_once_with(identity)

    async def test_a_refused_admission_releases_the_token(self, submit, release):
        identity = _identity()
        submit.side_effect = AdmissionError("Unknown MCP server: mine")

        with pytest.raises(AdmissionError):
            await CISubmission(identity).asubmit(self._target(), _payload(), prompt="/code-review")

        release.assert_awaited_once_with(identity)

    async def test_an_unexpected_failure_keeps_the_token_used(self, submit, release):
        submit.side_effect = RuntimeError("after the run")

        with pytest.raises(RuntimeError):
            await CISubmission(_identity()).asubmit(self._target(), _payload(), prompt="/code-review")

        release.assert_not_awaited()


@pytest.mark.parametrize(
    ("fields", "muted"), [({}, True), ({"muted": False}, False), ({"muted": True}, True)],
    ids=["unset", "unmuted", "muted"],
)  # fmt: skip
def test_a_submission_is_muted_unless_it_asks_otherwise(fields, muted):
    assert CISubmission.muted(_payload(**fields)) is muted


@pytest.mark.django_db(transaction=True)
class TestVisibleRuns:
    async def _run(self, **fields):
        session = await Session.objects.acreate(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id=fields.get("repo_id", "group/project")
        )
        return await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.API_JOB, status=RunStatus.SUCCESSFUL,
            **({"repo_id": "group/project"} | fields),
        )  # fmt: skip

    async def _visible(self, run, identity=None) -> bool:
        return await Run.objects.filter(CISubmission(identity or _identity()).visible_runs(), pk=run.pk).aexists()

    async def test_a_run_of_its_repository_attributed_to_its_actor_is_visible(self):
        run = await self._run(external_provider="gitlab", external_uid="9", repo_id="Group/Project")

        assert await self._visible(run)

    async def test_a_run_of_its_linked_actor_is_visible(self):
        user = await User.objects.acreate(username="linked", email="l@test.com")

        assert await self._visible(await self._run(user=user), _identity(user=user))

    async def test_a_job_on_an_owned_merge_request_is_visible_through_its_own_run(self):
        from tests.unit_tests.sessions.conftest import amake_owned_merge_request
        from tests.unit_tests.sessions.inbox.conftest import post_merge_request_job

        owned = await amake_owned_merge_request(repo_id="group/project", merge_request_iid=1)
        with patch("sessions.inbox.drain.Drain.start_logged"):
            run = await sync_to_async(post_merge_request_job)(owned)

        assert await self._visible(run)

    async def test_a_job_on_its_merge_request_in_a_session_of_several_repositories_is_visible(self):
        from tests.unit_tests.sessions.conftest import amake_owned_merge_request
        from tests.unit_tests.sessions.inbox.conftest import post_merge_request_job

        owned = await amake_owned_merge_request(repo_id="group/main", merge_request_iid=1)
        other = await MergeRequestOwnership.objects.acreate(
            repo_id="group/project", merge_request_iid=3, session_id=owned.session_id, source_branch="daiv/project"
        )
        with patch("sessions.inbox.drain.Drain.start_logged"):
            run = await sync_to_async(post_merge_request_job)(other)

        assert run.repo_id == "group/main"
        assert await self._visible(run)

    @pytest.mark.parametrize(
        "fields",
        [
            {"external_provider": "gitlab", "external_uid": "10"},
            {"external_provider": "github", "external_uid": "9"},
            {"external_provider": "gitlab", "external_uid": "9", "repo_id": "group/other"},
            {},
        ],
        ids=["another-actor", "another-platform", "another-repository", "no-one"],
    )
    async def test_any_other_run_is_not_visible(self, fields):
        assert not await self._visible(await self._run(**fields))
