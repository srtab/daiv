from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction

from asgiref.sync import sync_to_async

from automation.agent.references import MAX_REFS_PER_SUBMISSION
from sessions.admission import AdmissionError, aadmit, aassert_repo_access
from sessions.models import Run, RunStatus, Session, SessionRepository
from sessions.services import (
    BatchSubmitResult,
    RepoTarget,
    _aclaim_run,
    _aenqueue_batch_title,
    _astart_run,
    _asubmit_follow_up,
    asubmit_batch_runs,
    create_run,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import CharField

    from accounts.models import User
    from automation.agent.git_state import RepoPublish
    from automation.agent.references import RefIn

MAX_REPOS_PER_SESSION = 5
THREAD_NOT_FOUND = "thread_id not found"
THREAD_ARCHIVED = "thread_id is archived; unarchive the session to continue it"


class SharedSessionError(AdmissionError):
    """A shared-session submission the caller got wrong; the message is shown to them verbatim."""


async def ais_multi_repo(thread_id: str) -> bool:
    return await SessionRepository.objects.filter(session_id=thread_id).aexists()


async def acontinues_multi_repo_session(thread_id: str, run_id: str | None) -> bool:
    """Whether the run ``run_id`` continues a multi-repository session, whose stored branches a merge may have deleted
    since. The session's first run still carries the refs its caller typed, and those must exist."""
    if not await ais_multi_repo(thread_id):
        return False
    return await Run.objects.filter(session_id=thread_id).exclude(pk=run_id).aexists()


async def aload_extra_repos(thread_id: str) -> list[SessionRepository]:
    return [row async for row in SessionRepository.objects.filter(session_id=thread_id).order_by("position")]


async def apersist_extra_repo_state(thread_id: str, publishes: Sequence[RepoPublish]) -> None:
    """Keep each extra repository's working branch, MR (title, draft flag) and change size on its row, so the next
    turn continues there.

    A failed publish leaves its row alone; a blank ref is never written, since it means the default branch. A title
    longer than the column is cut to fit.
    """
    max_title_length = cast("CharField", SessionRepository._meta.get_field("merge_request_title")).max_length
    for rp in publishes:
        if rp.error:
            continue
        fields: dict[str, Any] = {}
        if rp.merge_request is not None:
            fields.update(
                ref=rp.merge_request.source_branch,
                merge_request_iid=rp.merge_request.merge_request_id,
                merge_request_web_url=rp.merge_request.web_url or "",
                merge_request_title=rp.merge_request.title[:max_title_length],
                merge_request_draft=rp.merge_request.draft,
            )
        elif not rp.published and rp.current_ref:
            fields["ref"] = rp.current_ref
        if rp.diff_stats is not None:
            fields["diff_stats"] = rp.diff_stats.model_dump()
        if fields:
            await SessionRepository.objects.filter(session_id=thread_id, repo_id=rp.repo_id).aupdate(**fields)


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
    """Create the run on ``repos[0]`` and a session row for each later one."""
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
    mcp_overrides: dict[str, bool] | None = None,
) -> BatchSubmitResult:
    """Admit and submit a jobs request: one run on a shared session when ``shared`` is set or ``thread_id`` continues
    a multi-repository session, else an independent run per repository.

    ``thread_id`` must name a session ``user`` owns and has not archived; the session then decides whether the
    continuation is shared, whatever ``shared`` says, so a one-repository shared session continues like any other
    single-repository one. Every refusal is an
    :class:`~sessions.admission.AdmissionError`. The result holds one outcome per entry of ``repos``, or a shared
    session's single one, for its main repository.
    """
    if thread_id is not None:
        owned = Session.objects.by_owner(user).filter(thread_id=thread_id)
        found = await owned.aexists()
        shared = found and await ais_multi_repo(thread_id)
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
        mcp_overrides=mcp_overrides,
        thread_id=thread_id,
    )
    submit = _asubmit_shared_session_run if shared else asubmit_batch_runs
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
        mcp_overrides=admission.mcp_overrides,
    )


async def _asubmit_shared_session_run(
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
    mcp_overrides: dict | None = None,
) -> BatchSubmitResult:
    """Submit one run over several repositories: a new shared session, or a continuation of one, which goes to its
    inbox like any follow-up job (:func:`sessions.services._asubmit_follow_up`) and follows the session's branch.

    For :func:`asubmit_jobs` only, which has already validated ``repos`` (:func:`validate_shared_repos`), admitted
    them, and checked that ``thread_id`` names a multi-repository session the user owns.
    """
    refs = [*(references or []), *(ref for t in repos for ref in t.references)]
    stored_refs = [ref.to_stored() for ref in refs]
    if len(stored_refs) > MAX_REFS_PER_SUBMISSION:
        raise SharedSessionError(f"references exceeds the maximum of {MAX_REFS_PER_SUBMISSION}")

    batch_id = uuid.uuid4()
    if thread_id is not None:
        session = await Session.objects.aget(pk=thread_id)
        stored_ids = await session.arepo_ids()
        _validate_continuation(repos, stored_ids)
        await aassert_repo_access(user, stored_ids)
        follow_up = await _asubmit_follow_up(
            session,
            RepoTarget(session.repo_id, "", repos[0].sandbox_environment_id),
            user=user,
            prompt=prompt,
            trigger_type=trigger_type,
            agent_model=agent_model,
            agent_thinking_level=agent_thinking_level,
            muted=muted,
            references=stored_refs,
            mcp_overrides=mcp_overrides,
        )
        return BatchSubmitResult(batch_id=batch_id, outcomes=[follow_up], shared=True)

    run_kwargs = {
        "trigger_type": trigger_type,
        "prompt": prompt,
        "agent_model": agent_model,
        "agent_thinking_level": agent_thinking_level,
        "muted": muted,
        "batch_id": batch_id,
        "mcp_overrides": mcp_overrides,
    }

    def create(status: str):
        return sync_to_async(_create_session_run)(
            repos=repos, thread_id=str(uuid.uuid4()), status=status, run_kwargs=run_kwargs, user=user
        )

    log_context = "submit_shared_session_run"
    claimed = await _aclaim_run(
        create,
        target=repos[0],
        initial_status=RunStatus.READY,
        stored_refs=stored_refs,
        log_context=log_context,
        mcp_overrides=mcp_overrides,
    )
    result = BatchSubmitResult(
        batch_id=batch_id, outcomes=[await _astart_run(claimed, log_context=log_context)], shared=True
    )
    await _aenqueue_batch_title(
        batch_id=batch_id, prompt=prompt, user=user, trigger_type=trigger_type, runs=result.runs
    )
    return result
