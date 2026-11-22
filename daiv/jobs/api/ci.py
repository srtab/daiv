"""What a job submission authenticated by a CI identity token may do, beyond what an API key's may."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Exists, OuterRef, Q

from asgiref.sync import sync_to_async
from sessions.merge_request_runs.starter import Requester
from sessions.models import EventKind, InboxEvent

from accounts.api.ci_identity import CIIdentityRefused, CIIdentityTokens, CIIdentityVerifier
from codebase.repo_config import RepositoryConfig

if TYPE_CHECKING:
    from accounts.api.ci_identity import CIIdentity

    from .schemas import JobSubmitRequest

ONE_JOB_ON_ITS_REPOSITORY = "A CI identity token submits one job on its own repository."
UNAUTHORIZED = {"detail": "Unauthorized"}


class CISubmission:
    """A job submission from the pipeline ``identity`` names: one job on the pipeline's own repository, attributed to
    the account that triggered it, acting as no one, muted unless it asks otherwise, once per token."""

    def __init__(self, identity: CIIdentity):
        self.identity = identity

    async def arefusal(self, payload: JobSubmitRequest) -> tuple[int, dict] | None:
        """Why ``payload`` is refused, as a response, or ``None``; claims the token when it is not. Each ``401`` is
        logged with the check it failed."""
        identity = self.identity
        if len(payload.repos) != 1 or payload.thread_id is not None or payload.shared_session or payload.references:
            return 400, {"detail": ONE_JOB_ON_ITS_REPOSITORY}
        repo_id = payload.repos[0].repo_id
        try:
            repository = await CIIdentityVerifier(identity.provider).averify_repository(identity, repo_id)
        except CIIdentityRefused as refused:
            refused.log()
            return 401, UNAUTHORIZED
        config = await sync_to_async(RepositoryConfig.get_config)(repo_id, repository=repository)
        if not config.is_user_allowed(identity.actor_username):
            CIIdentityRefused("allowed usernames", identity.repo_id).log()
            return 401, UNAUTHORIZED
        if not await CIIdentityTokens.aclaim(identity):
            return 401, UNAUTHORIZED
        return None

    async def arelease(self) -> None:
        """Let the token submit again, after its submission accepted no job."""
        await CIIdentityTokens.arelease(self.identity)

    @property
    def requester(self) -> Requester:
        identity = self.identity
        return Requester(
            user=identity.user,
            provider=identity.provider,
            uid=identity.actor_uid,
            username=identity.actor_username,
            acts_as_user=False,
        )

    @staticmethod
    def muted(payload: JobSubmitRequest) -> bool:
        return payload.muted if "muted" in payload.model_fields_set else True

    def visible_runs(self) -> Q:
        """The runs the token may read: on its repository, or a job on one of its merge requests that a session of
        several repositories runs, and attributed to its triggering account."""
        identity = self.identity
        person = Q(external_provider=identity.provider, external_uid=identity.actor_uid)
        if identity.user is not None:
            person |= Q(user=identity.user)
        job_on_its_merge_request = InboxEvent.objects.filter(
            run=OuterRef("pk"), kind=EventKind.MERGE_REQUEST_JOB, repo_id__iexact=identity.repo_id
        )
        return (Q(repo_id__iexact=identity.repo_id) | Q(Exists(job_on_its_merge_request))) & person
