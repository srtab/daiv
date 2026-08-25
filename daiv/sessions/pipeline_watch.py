import logging
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from django.utils import timezone
from django.utils.translation import gettext as _

from asgiref.sync import sync_to_async

from codebase.base import Scope
from codebase.clients import RepoClient
from codebase.repo_config import RepositoryConfig
from codebase.utils import compute_thread_id
from sessions.models import RunStatus, Session, SessionOrigin, WatchState

if TYPE_CHECKING:
    from codebase.base import Job, Pipeline

logger = logging.getLogger("daiv.sessions")

TERMINAL_PIPELINE_STATUSES = frozenset({"success", "failed", "canceled", "skipped"})
NEEDS_A_HUMAN_STATUSES = frozenset({"blocked", "manual", "canceled", "skipped"})

WATCH_MAX_AGE = timedelta(hours=6)
WATCH_STALE_AFTER = timedelta(minutes=30)


class Judgment(StrEnum):
    GREEN = "green"
    ACTIONABLE = "actionable"
    UNCLEAR = "unclear"


def failed_jobs(pipeline: Pipeline) -> list[Job]:
    """Jobs whose failure the project has not declared acceptable."""
    return [job for job in pipeline.jobs if job.is_failed() and not job.allow_failure]


def judge_pipeline(pipeline: Pipeline | None) -> Judgment:
    """Decide whether a pipeline is green, worth an agent run, or not ours to judge.

    Deliberately conservative: anything that is not an unambiguous pass or an unambiguous
    failure is ``UNCLEAR``, which stops the watch instead of spending an attempt.
    """
    if pipeline is None:
        return Judgment.UNCLEAR
    if pipeline.status in NEEDS_A_HUMAN_STATUSES:
        return Judgment.UNCLEAR
    if pipeline.status not in TERMINAL_PIPELINE_STATUSES:
        return Judgment.UNCLEAR
    if not pipeline.jobs:
        return Judgment.UNCLEAR
    if pipeline.status == "failed":
        real_failures = failed_jobs(pipeline)
        # A failed pipeline whose only failed jobs are all allow_failure is effectively green.
        # A pipeline with no failed jobs visible (e.g. config error) is still worth a look.
        if real_failures or not any(job.is_failed() for job in pipeline.jobs):
            return Judgment.ACTIONABLE
        return Judgment.GREEN
    return Judgment.GREEN


async def _aread_pipeline(*, repo_id: str, ref: str, pipeline_id: int | None) -> Pipeline | None:
    client = RepoClient.create_instance()
    if pipeline_id is not None:
        return await sync_to_async(client.get_pipeline)(repo_id, pipeline_id)
    return await sync_to_async(client.get_latest_pipeline_for_ref)(repo_id, ref)


async def _apost_watch_note(*, repo_id: str, merge_request_iid: int, body: str) -> None:
    try:
        client = RepoClient.create_instance()
        await sync_to_async(client.create_merge_request_comment)(repo_id, merge_request_iid, body)
    except Exception:
        logger.exception("pipeline_watch: failed to comment on %s!%s", repo_id, merge_request_iid)


async def _adispatch_fix_run(*, session: Session, pipeline: Pipeline, repo_id: str, merge_request_iid: int) -> None:
    from jobs.tasks import run_job_task

    from sessions.services import acreate_run

    names = ", ".join(job.name for job in failed_jobs(pipeline)) or _("the pipeline")
    prompt = _(
        "CI failed on this merge request. Failed jobs: {names}. Pipeline: {url}\n\n"
        "Read the job logs to find the cause, then fix it. If the failure is not something a "
        "code change can fix, explain why instead of guessing."
    ).format(names=names, url=pipeline.web_url)

    result = await run_job_task.aenqueue(
        repo_id=repo_id, prompt=prompt, thread_id=session.thread_id, ref=session.ref, user_id=session.user_id
    )
    await acreate_run(
        trigger_type=SessionOrigin.PIPELINE_WEBHOOK,
        task_result_id=result.id,
        repo_id=repo_id,
        ref=session.ref,
        prompt=prompt,
        merge_request_iid=merge_request_iid,
        thread_id=session.thread_id,
        user=await sync_to_async(lambda: session.user)(),
        status=RunStatus.QUEUED,
    )


async def anotify_watch_exhausted(*, session: Session, pipeline: Pipeline) -> None:
    """Replaced in Task 9."""
    return None


async def aarm_watch(
    *, repo_id: str, merge_request_iid: int, ref: str, source_thread_id: str, was_fix_run: bool
) -> str | None:
    """Point the watch at a merge request DAIV just published.

    Returns the MR thread id it armed, or ``None`` when the repo has the feature off. The
    counter survives re-arming by a fix run — that is what bounds the loop.
    """
    config = await sync_to_async(RepositoryConfig.get_config)(repo_id)
    if not config.pipeline_watch.enabled:
        return None

    thread_id = compute_thread_id(repo_slug=repo_id, scope=Scope.MERGE_REQUEST, entity_iid=merge_request_iid)
    _session, _created = await Session.objects.aget_or_create(
        thread_id=thread_id,
        defaults={
            "origin": SessionOrigin.PIPELINE_WEBHOOK,
            "repo_id": repo_id,
            "ref": ref,
            "merge_request_iid": merge_request_iid,
        },
    )
    updates = {
        "watch_state": WatchState.WATCHING,
        "watch_armed_at": timezone.now(),
        "ref": ref,
        "merge_request_iid": merge_request_iid,
    }
    if not was_fix_run:
        updates["watch_attempts"] = 0
        updates["watch_pipeline_id"] = None
    await Session.objects.filter(thread_id=thread_id).aupdate(**updates)
    return thread_id


async def aexhaust_watch(*, repo_id: str, merge_request_iid: int, reason: str) -> None:
    """End a watch that cannot make progress, whatever state it is in.

    Used when a fix run produced no diff: no push means no pipeline and therefore no event,
    so nothing else would ever move this watch.
    """
    thread_id = compute_thread_id(repo_slug=repo_id, scope=Scope.MERGE_REQUEST, entity_iid=merge_request_iid)
    updated = await Session.objects.filter(thread_id=thread_id, watch_state__in=WatchState.open()).aupdate(
        watch_state=WatchState.EXHAUSTED
    )
    if not updated:
        return
    await _apost_watch_note(
        repo_id=repo_id,
        merge_request_iid=merge_request_iid,
        body=_("I stopped watching CI on this merge request: {reason}.").format(reason=reason),
    )


async def aevaluate_watch(*, repo_id: str, ref: str, pipeline_id: int | None) -> None:
    """Judge the pipeline for a watched branch and take the one action it implies."""
    session = (
        await Session.objects
        .filter(repo_id=repo_id, ref=ref, watch_state=WatchState.WATCHING)
        .order_by("-watch_armed_at")
        .afirst()
    )
    if session is None:
        return
    if pipeline_id is not None and session.watch_pipeline_id == pipeline_id:
        return

    pipeline = await _aread_pipeline(repo_id=repo_id, ref=ref, pipeline_id=pipeline_id)
    judgment = judge_pipeline(pipeline)
    merge_request_iid = session.merge_request_iid

    if judgment is Judgment.GREEN:
        await Session.objects.filter(thread_id=session.thread_id).aupdate(
            watch_state=WatchState.GREEN, watch_pipeline_id=pipeline.id
        )
        return

    if judgment is Judgment.UNCLEAR:
        await Session.objects.filter(thread_id=session.thread_id).aupdate(watch_state=WatchState.UNCLEAR)
        if merge_request_iid:
            await _apost_watch_note(
                repo_id=repo_id,
                merge_request_iid=merge_request_iid,
                body=_(
                    "I stopped watching CI on this branch: the pipeline did not finish in a way I "
                    "can act on (status `{status}`). Re-run it or mention me if you want another look."
                ).format(status=pipeline.status if pipeline else "missing"),
            )
        return

    config = await sync_to_async(RepositoryConfig.get_config)(repo_id)
    if session.watch_attempts >= config.pipeline_watch.max_attempts:
        await Session.objects.filter(thread_id=session.thread_id).aupdate(
            watch_state=WatchState.EXHAUSTED, watch_pipeline_id=pipeline.id
        )
        if merge_request_iid:
            names = ", ".join(job.name for job in failed_jobs(pipeline)) or _("the pipeline")
            await _apost_watch_note(
                repo_id=repo_id,
                merge_request_iid=merge_request_iid,
                body=_(
                    "I spent {attempts} attempts trying to get CI green and did not manage it. "
                    "Still failing: {names}. Pipeline: {url}"
                ).format(attempts=session.watch_attempts, names=names, url=pipeline.web_url),
            )
        await anotify_watch_exhausted(session=session, pipeline=pipeline)
        return

    await Session.objects.filter(thread_id=session.thread_id).aupdate(
        watch_state=WatchState.FIXING, watch_attempts=session.watch_attempts + 1, watch_pipeline_id=pipeline.id
    )
    await session.arefresh_from_db()
    await _adispatch_fix_run(session=session, pipeline=pipeline, repo_id=repo_id, merge_request_iid=merge_request_iid)
