from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from django.db import transaction

from asgiref.sync import sync_to_async

from automation.agent.references import MAX_REFS_PER_SUBMISSION
from sessions.admission import aassert_repo_access
from sessions.models import Session, SessionRepository
from sessions.services import (
    BatchSubmitFailure,
    BatchSubmitResult,
    RepoTarget,
    _aclaim_and_enqueue,
    _aenqueue_batch_title,
    create_run,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from accounts.models import User
    from automation.agent.publishers import RepoPublish
    from automation.agent.references import RefIn
    from sessions.models import Run

MAX_REPOS_PER_SESSION = 5


class SharedSessionError(ValueError):
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
        rows = SessionRepository.objects.filter(session_id=thread_id, repo_id=rp.repo_id)
        if rp.error:
            continue
        if rp.merge_request is not None:
            await rows.aupdate(
                ref=rp.merge_request.source_branch,
                merge_request_iid=rp.merge_request.merge_request_id,
                merge_request_web_url=rp.merge_request.web_url or "",
            )
        elif not rp.published and rp.current_ref:
            await rows.aupdate(ref=rp.current_ref)


def _validate_new(repos: list[RepoTarget]) -> None:
    if not repos:
        raise SharedSessionError("repos must contain at least one entry")
    if len(repos) > MAX_REPOS_PER_SESSION:
        raise SharedSessionError(f"a shared session holds at most {MAX_REPOS_PER_SESSION} repositories")
    repo_ids = [target.repo_id for target in repos]
    duplicates = sorted({repo_id for repo_id in repo_ids if repo_ids.count(repo_id) > 1})
    if duplicates:
        raise SharedSessionError(f"repos lists the same repository more than once: {', '.join(duplicates)}")


def _validate_continuation(repos: list[RepoTarget], stored_ids: list[str]) -> None:
    if any(target.ref for target in repos):
        raise SharedSessionError("a continuation of a shared session cannot set a ref; it reuses the stored branches")
    sent = [target.repo_id for target in repos]
    if sent != stored_ids[:1] and sent != stored_ids:
        raise SharedSessionError(
            "a continuation of a shared session must send the main repository alone or the same repositories "
            f"in the same order: {', '.join(stored_ids)}"
        )


def _create_new_session_run(
    *, repos: list[RepoTarget], thread_id: str, status: str, run_kwargs: dict, user: User
) -> Run:
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
    stored_refs = [ref.to_stored() for ref in references or []]
    if len(stored_refs) > MAX_REFS_PER_SUBMISSION:
        raise SharedSessionError(f"references exceeds the maximum of {MAX_REFS_PER_SUBMISSION}")

    run_kwargs = {
        "trigger_type": trigger_type,
        "prompt": prompt,
        "agent_model": agent_model,
        "agent_thinking_level": agent_thinking_level,
        "muted": muted,
    }
    batch_id = uuid.uuid4()

    if thread_id is None:
        _validate_new(repos)
        await aassert_repo_access(user, [target.repo_id for target in repos])
        target = repos[0]
        new_thread_id = str(uuid.uuid4())

        def create(status: str):
            return sync_to_async(_create_new_session_run)(
                repos=repos,
                thread_id=new_thread_id,
                status=status,
                run_kwargs=run_kwargs | {"batch_id": batch_id},
                user=user,
            )

    else:
        try:
            uuid.UUID(thread_id)
        except (ValueError, TypeError) as err:
            raise SharedSessionError("thread_id must be a UUID string") from err
        session = await Session.objects.filter(pk=thread_id).afirst()
        extras = await aload_extra_repos(thread_id) if session else []
        if not extras:
            raise SharedSessionError(f"thread {thread_id} is not a multi-repository session")
        stored_ids = [session.repo_id, *(row.repo_id for row in extras)]
        _validate_continuation(repos, stored_ids)
        await aassert_repo_access(user, stored_ids)
        target = RepoTarget(session.repo_id, session.ref, repos[0].sandbox_environment_id)

        def create(status: str):
            return sync_to_async(create_run)(
                **run_kwargs,
                batch_id=batch_id,
                user=user,
                repo_id=target.repo_id,
                ref=target.ref,
                sandbox_environment_id=target.sandbox_environment_id,
                thread_id=thread_id,
                task_result_id=None,
                status=status,
            )

    outcome = await _aclaim_and_enqueue(
        create, target=target, stored_refs=stored_refs, log_context="submit_shared_session_run"
    )
    if isinstance(outcome, BatchSubmitFailure):
        return BatchSubmitResult(batch_id=batch_id, failed=[outcome])
    await _aenqueue_batch_title(batch_id=batch_id, prompt=prompt, user=user, trigger_type=trigger_type, runs=[outcome])
    return BatchSubmitResult(batch_id=batch_id, runs=[outcome])
