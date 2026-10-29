from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from django.db import transaction

from asgiref.sync import sync_to_async

from automation.agent.references import MAX_REFS_PER_SUBMISSION
from sessions.admission import AdmissionError, aadmit, aassert_repo_access
from sessions.models import RunStatus, Session, SessionRepository
from sessions.services import (
    BatchSubmitResult,
    RepoTarget,
    _aclaim_run,
    _aenqueue_batch_title,
    _astart_run,
    asubmit_batch_runs,
    create_run,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from accounts.models import User
    from automation.agent.git_state import RepoPublish
    from automation.agent.references import RefIn
    from sessions.models import Run

MAX_REPOS_PER_SESSION = 5
THREAD_NOT_FOUND = "thread_id not found"
THREAD_ARCHIVED = "thread_id is archived; unarchive the session to continue it"


class SharedSessionError(AdmissionError):
    """A shared-session submission the caller got wrong; the message is shown to them verbatim."""


async def ais_multi_repo(thread_id: str) -> bool:
    return await SessionRepository.objects.filter(session_id=thread_id).aexists()


async def aload_extra_repos(thread_id: str) -> list[SessionRepository]:
    return [row async for row in SessionRepository.objects.filter(session_id=thread_id).order_by("position")]


async def apersist_extra_repo_state(thread_id: str, publishes: Sequence[RepoPublish]) -> None:
    """Keep each extra repository's working branch and MR on its row, so the next turn continues there.

    A failed publish leaves its row alone; a blank ref is never written, since it means the default branch.
    """
    for rp in publishes:
        if rp.error:
            continue
        rows = SessionRepository.objects.filter(session_id=thread_id, repo_id=rp.repo_id)
        if rp.merge_request is not None:
            await rows.aupdate(
                ref=rp.merge_request.source_branch,
                merge_request_iid=rp.merge_request.merge_request_id,
                merge_request_web_url=rp.merge_request.web_url or "",
            )
        elif not rp.published and rp.current_ref:
            await rows.aupdate(ref=rp.current_ref)


def validate_shared_repos(repos: Sequence[RepoTarget], *, continuation: bool) -> None:
    """Check the shape of a shared-session list, without reading anything; run it before admission."""
    if not repos:
        raise SharedSessionError("repos must contain at least one entry")
    if len(repos) > MAX_REPOS_PER_SESSION:
        raise SharedSessionError(f"a shared session holds at most {MAX_REPOS_PER_SESSION} repositories")
    repo_ids = [target.repo_id for target in repos]
    duplicates = sorted({repo_id for repo_id in repo_ids if repo_ids.count(repo_id) > 1})
    if duplicates:
        raise SharedSessionError(f"repos lists the same repository more than once: {', '.join(duplicates)}")
    if continuation and any(target.ref for target in repos):
        raise SharedSessionError("a continuation of a shared session cannot set a ref; it reuses the stored branches")
    if any(target.prompt for target in repos):
        raise SharedSessionError(
            "a shared session runs one prompt for all its repositories; repos[].prompt cannot be set"
        )


def _validate_continuation(repos: list[RepoTarget], stored_ids: list[str]) -> None:
    sent = [target.repo_id for target in repos]
    if sent != stored_ids[:1] and sent != stored_ids:
        raise SharedSessionError(
            "a continuation of a shared session must send the main repository alone or the same repositories "
            f"in the same order: {', '.join(stored_ids)}"
        )


def _create_session_run(*, repos: list[RepoTarget], thread_id: str, status: str, run_kwargs: dict, user: User) -> Run:
    """Create the run on ``repos[0]`` and a session row for each later one; a continuation passes the main alone."""
    main, *extras = repos
    with transaction.atomic():
        run = create_run(
            **run_kwargs,
            user=user,
            repo_id=main.repo_id,
            ref=main.ref,
            sandbox_environment_id=main.sandbox_environment_id,
            thread_id=thread_id,
            task_result_id=None,
            status=status,
        )
        SessionRepository.objects.bulk_create([
            SessionRepository(
                session=run.session,
                position=position,
                repo_id=target.repo_id,
                ref=target.ref,
                sandbox_environment_id=target.sandbox_environment_id,
            )
            for position, target in enumerate(extras, start=1)
        ])
    return run


async def asubmit_jobs(
    *,
    user: User,
    repos: list[RepoTarget],
    shared: bool,
    thread_id: str | None,
    prompt: str,
    agent_model: str | None,
    agent_thinking_level: str | None,
    environment: str | None,
    muted: bool | None,
    trigger_type: str,
    references: Sequence[RefIn] | None,
) -> BatchSubmitResult:
    """Admit and submit a jobs request: one run on a shared session when ``shared`` is set or ``thread_id`` continues
    a multi-repository session, else an independent run per repository.

    ``thread_id`` must name a session ``user`` owns and has not archived. Every refusal is an
    :class:`~sessions.admission.AdmissionError`. The result holds one outcome per entry of ``repos``, or a shared
    session's single one, for its main repository.
    """
    if thread_id is not None:
        owned = Session.objects.by_owner(user).filter(thread_id=thread_id)
        found = await owned.aexists()
        shared = shared or (found and await ais_multi_repo(thread_id))
        if not shared and len(repos) != 1:
            raise AdmissionError("thread_id continuation requires exactly one repo")
        if not found:
            raise AdmissionError(THREAD_NOT_FOUND)
        if await owned.archived_for(user).aexists():
            raise AdmissionError(THREAD_ARCHIVED)
    if shared:
        validate_shared_repos(repos, continuation=thread_id is not None)
    admission = await aadmit(
        user=user,
        repos=repos,
        agent_model=agent_model,
        agent_thinking_level=agent_thinking_level,
        environment=environment,
    )
    submit = asubmit_shared_session_run if shared else asubmit_batch_runs
    return await submit(
        user=user,
        prompt=prompt,
        repos=admission.targets,
        agent_model=admission.agent_model,
        agent_thinking_level=admission.agent_thinking_level,
        muted=muted,
        trigger_type=trigger_type,
        thread_id=thread_id,
        references=references,
    )


async def asubmit_shared_session_run(
    *,
    user: User,
    prompt: str,
    repos: list[RepoTarget],
    agent_model: str = "",
    agent_thinking_level: str = "",
    muted: bool | None = None,
    trigger_type: str,
    thread_id: str | None = None,
    references: Sequence[RefIn] | None = None,
) -> BatchSubmitResult:
    """Submit one run over several repositories: a new shared session, or a continuation of one."""
    validate_shared_repos(repos, continuation=thread_id is not None)
    refs = [*(references or []), *(ref for t in repos for ref in t.references)]
    stored_refs = [ref.to_stored() for ref in refs]
    if len(stored_refs) > MAX_REFS_PER_SUBMISSION:
        raise SharedSessionError(f"references exceeds the maximum of {MAX_REFS_PER_SUBMISSION}")

    batch_id = uuid.uuid4()
    run_kwargs = {
        "trigger_type": trigger_type,
        "prompt": prompt,
        "agent_model": agent_model,
        "agent_thinking_level": agent_thinking_level,
        "muted": muted,
        "batch_id": batch_id,
    }

    if thread_id is None:
        await aassert_repo_access(user, [target.repo_id for target in repos])
        session_repos, session_thread_id = repos, str(uuid.uuid4())
    else:
        try:
            uuid.UUID(thread_id)
        except (ValueError, TypeError) as err:
            raise SharedSessionError("thread_id must be a UUID string") from err
        session = await Session.objects.filter(pk=thread_id).afirst()
        stored_ids = await session.arepo_ids() if session else []
        if len(stored_ids) < 2:
            raise SharedSessionError(f"thread {thread_id} is not a multi-repository session")
        _validate_continuation(repos, stored_ids)
        await aassert_repo_access(user, stored_ids)
        session_repos = [RepoTarget(session.repo_id, session.ref, repos[0].sandbox_environment_id)]
        session_thread_id = thread_id

    def create(status: str):
        return sync_to_async(_create_session_run)(
            repos=session_repos, thread_id=session_thread_id, status=status, run_kwargs=run_kwargs, user=user
        )

    log_context = "submit_shared_session_run"
    claimed = await _aclaim_run(
        create,
        target=session_repos[0],
        initial_status=RunStatus.READY,
        stored_refs=stored_refs,
        log_context=log_context,
    )
    result = BatchSubmitResult(batch_id=batch_id, outcomes=[await _astart_run(claimed, log_context=log_context)])
    await _aenqueue_batch_title(
        batch_id=batch_id, prompt=prompt, user=user, trigger_type=trigger_type, runs=result.runs
    )
    return result
