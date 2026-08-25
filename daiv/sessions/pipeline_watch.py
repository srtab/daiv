import logging
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext as _

from asgiref.sync import sync_to_async
from notifications.channels.registry import enabled_channels
from notifications.choices import EventType
from notifications.services import notify
from sandbox_envs.services import resolve_env_for_run

from codebase.base import Scope
from codebase.clients import RepoClient
from codebase.repo_config import RepositoryConfig
from codebase.utils import compute_thread_id
from core.site_settings import site_settings
from sessions.models import RunStatus, Session, SessionOrigin, WatchState

if TYPE_CHECKING:
    from codebase.base import Job, Pipeline

logger = logging.getLogger("daiv.sessions")

TERMINAL_PIPELINE_STATUSES = frozenset({"success", "failed", "canceled", "skipped"})
NEEDS_A_HUMAN_STATUSES = frozenset({"blocked", "manual", "canceled", "skipped"})
# Statuses ``judge_pipeline`` can be trusted on. Everything else (created, pending, running,
# preparing, …) is not a verdict yet, only a pipeline that has not got there.
JUDGEABLE_PIPELINE_STATUSES = TERMINAL_PIPELINE_STATUSES | NEEDS_A_HUMAN_STATUSES

WATCH_MAX_AGE = timedelta(hours=6)
WATCH_STALE_AFTER = timedelta(minutes=30)


class Judgment(StrEnum):
    GREEN = "green"
    ACTIONABLE = "actionable"
    UNCLEAR = "unclear"


def watch_enabled(config: RepositoryConfig) -> bool:
    """Whether the watch may run for a repo. The site switch is a ceiling, not a default: a
    repository can turn the watch off but never turn one on that the operator disabled."""
    return bool(config.pipeline_watch.enabled and site_settings.pipeline_watch_enabled)


def watch_max_attempts(config: RepositoryConfig) -> int:
    """The attempt cap for a repo, clamped to the site-wide value — a repository can only tighten."""
    return min(config.pipeline_watch.max_attempts, site_settings.pipeline_watch_max_attempts)


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
    """Create the fix Run, then enqueue the agent task against it.

    Create-then-enqueue is not cosmetic: ``run_job_task`` reads ``trigger_type`` off the Run row
    to know a fix run is re-arming the watch, which is what keeps ``watch_attempts`` from resetting.
    """
    from jobs.tasks import run_job_task

    from sessions.services import acreate_run

    names = ", ".join(job.name for job in failed_jobs(pipeline)) or _("the pipeline")
    prompt = _(
        "CI failed on this merge request. Failed jobs: {names}. Pipeline: {url}\n\n"
        "Read the job logs to find the cause, then fix it. If the failure is not something a "
        "code change can fix, explain why instead of guessing."
    ).format(names=names, url=pipeline.web_url)

    user = await sync_to_async(lambda: session.user)()
    sandbox_env = await resolve_env_for_run(user=user, repo_id=repo_id)
    sandbox_environment_id = str(sandbox_env.id) if sandbox_env is not None else None

    run = await acreate_run(
        trigger_type=SessionOrigin.PIPELINE_WEBHOOK,
        task_result_id=None,
        repo_id=repo_id,
        ref=session.ref,
        prompt=prompt,
        merge_request_iid=merge_request_iid,
        thread_id=session.thread_id,
        user=user,
        sandbox_environment_id=sandbox_environment_id,
        status=RunStatus.READY,
    )
    result = await run_job_task.aenqueue(
        repo_id=repo_id,
        prompt=prompt,
        thread_id=session.thread_id,
        ref=session.ref,
        sandbox_environment_id=sandbox_environment_id,
        run_id=str(run.pk),
        user_id=session.user_id,
    )
    try:
        run.task_result_id = result.id
        await run.asave(update_fields=["task_result_id"])
    except Exception:
        logger.exception(
            "pipeline_watch: failed to link task_result_id=%s to run=%s (the fix run still executes)", result.id, run.pk
        )


async def anotify_watch_exhausted(*, session: Session, pipeline: Pipeline) -> None:
    """Tell the session owner the watch gave up. Best-effort: the MR comment is the real channel."""
    user = await sync_to_async(lambda: session.user)()
    if user is None:
        logger.warning(
            "pipeline_watch: no recipient for exhausted watch on %s (thread_id=%s)", session.repo_id, session.thread_id
        )
        return

    names = ", ".join(job.name for job in failed_jobs(pipeline)) or _("the pipeline")
    try:
        await sync_to_async(notify)(
            recipient=user,
            event_type=EventType.PIPELINE_WATCH_EXHAUSTED,
            source_type="session",
            source_id=session.thread_id,
            subject=_("CI still failing on {repo}!{iid}").format(repo=session.repo_id, iid=session.merge_request_iid),
            body=_("I could not get CI green after {attempts} attempts. Still failing: {names}.").format(
                attempts=session.watch_attempts, names=names
            ),
            link_url=pipeline.web_url,
            channels=[cls.channel_type for cls in enabled_channels()],
        )
    except Exception:
        logger.exception("pipeline_watch: failed to notify %s", user.pk)


async def aarm_watch(*, repo_id: str, merge_request_iid: int, ref: str, was_fix_run: bool) -> str | None:
    """Point the watch at a merge request DAIV just published.

    Returns the MR thread id it armed, or ``None`` when the repo has the feature off. The
    counter survives re-arming by a fix run — that is what bounds the loop.
    """
    config = await sync_to_async(RepositoryConfig.get_config)(repo_id)
    if not watch_enabled(config):
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


async def _apolled_pipeline_is_current(*, session: Session, pipeline: Pipeline, repo_id: str) -> bool:
    """Whether a *polled* pipeline belongs to the merge request's current head commit.

    ``get_latest_pipeline_for_ref`` correlates with nothing, so without this a terminal pipeline
    from an earlier push is judged: a stale ``success`` closes the watch, a stale ``failed`` spends
    an attempt on something already fixed. An unreadable head sha means "cannot correlate", which
    is a skip — the webhook path and the next sweep are unaffected.
    """
    if not session.merge_request_iid:
        return False
    try:
        client = RepoClient.create_instance()
        merge_request = await sync_to_async(client.get_merge_request)(repo_id, session.merge_request_iid)
    except Exception:
        logger.exception("pipeline_watch: could not read head sha for %s!%s", repo_id, session.merge_request_iid)
        return False
    return bool(merge_request.sha) and pipeline.sha == merge_request.sha


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
    if pipeline is None or pipeline.status not in JUDGEABLE_PIPELINE_STATUSES:
        # Not judgeable *yet* — ``judge_pipeline`` reads a still-running pipeline as UNCLEAR, and
        # the arm-time evaluation runs seconds after the push. WATCH_MAX_AGE is the backstop.
        logger.debug(
            "pipeline_watch: nothing to judge yet on %s@%s (status=%s)",
            repo_id,
            ref,
            pipeline.status if pipeline else "missing",
        )
        return
    if pipeline.id == session.watch_pipeline_id:
        return
    if pipeline_id is None and not await _apolled_pipeline_is_current(
        session=session, pipeline=pipeline, repo_id=repo_id
    ):
        logger.info("pipeline_watch: polled pipeline %s is not the head of %s@%s", pipeline.id, repo_id, ref)
        return

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
                ).format(status=pipeline.status),
            )
        return

    config = await sync_to_async(RepositoryConfig.get_config)(repo_id)
    if session.watch_attempts >= watch_max_attempts(config):
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

    # Compare-and-swap out of WATCHING: two events finishing together both pass the cap check
    # off their own stale read, and only the row-count tells us which one owns the attempt.
    claimed = await Session.objects.filter(thread_id=session.thread_id, watch_state=WatchState.WATCHING).aupdate(
        watch_state=WatchState.FIXING,
        watch_attempts=F("watch_attempts") + 1,
        watch_pipeline_id=pipeline.id,
        watch_armed_at=timezone.now(),
    )
    if claimed != 1:
        return
    await session.arefresh_from_db()
    await _adispatch_fix_run(session=session, pipeline=pipeline, repo_id=repo_id, merge_request_iid=merge_request_iid)


async def areconcile_watches() -> int:
    """Repair watches that events did not resolve.

    Three jobs, in this order: expire watches past their lifetime, un-stick a watch whose fix
    run died, and re-judge branches whose event never arrived. Returns how many it touched.
    """
    now = timezone.now()
    touched = 0

    expired = Session.objects.filter(watch_state__in=WatchState.open(), watch_armed_at__lt=now - WATCH_MAX_AGE)
    async for session in expired:
        await Session.objects.filter(thread_id=session.thread_id).aupdate(watch_state=WatchState.UNCLEAR)
        touched += 1

    stuck = Session.objects.filter(
        watch_state=WatchState.FIXING,
        watch_armed_at__gte=now - WATCH_MAX_AGE,
        watch_armed_at__lt=now - WATCH_STALE_AFTER,
    )
    async for session in stuck:
        await Session.objects.filter(thread_id=session.thread_id).aupdate(
            watch_state=WatchState.WATCHING, watch_armed_at=now
        )
        touched += 1

    stale = Session.objects.filter(
        watch_state=WatchState.WATCHING,
        watch_armed_at__gte=now - WATCH_MAX_AGE,
        watch_armed_at__lt=now - WATCH_STALE_AFTER,
    )
    async for session in stale:
        try:
            await aevaluate_watch(repo_id=session.repo_id, ref=session.ref, pipeline_id=None)
        except Exception:
            logger.exception("pipeline_watch: reconcile failed for thread_id=%s", session.thread_id)
        touched += 1

    return touched
