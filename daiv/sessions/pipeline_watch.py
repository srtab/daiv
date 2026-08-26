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

from chat.repo_state import mr_to_payload
from codebase.base import Scope
from codebase.clients import RepoClient
from codebase.repo_config import RepositoryConfig
from codebase.utils import compute_thread_id
from core.site_settings import site_settings
from sessions.locks import stale_cutoff
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState

if TYPE_CHECKING:
    from codebase.base import Job, MergeRequest, Pipeline

logger = logging.getLogger("daiv.sessions")

# The only two statuses that are a verdict. Everything else either waits on a human or has
# not got there yet (created, pending, running, preparing, …), and both read as UNCLEAR.
VERDICT_STATUSES = frozenset({"success", "failed"})
NEEDS_A_HUMAN_STATUSES = frozenset({"blocked", "manual", "canceled", "skipped"})
JUDGEABLE_PIPELINE_STATUSES = VERDICT_STATUSES | NEEDS_A_HUMAN_STATUSES

WATCH_MAX_AGE = timedelta(hours=6)
# How long to wait for a pipeline event before polling the branch ourselves. Unrelated to
# ``STALE_RUN_MINUTES``, which is what says a *fix run* died — see ``areconcile_watches``.
WATCH_STALE_AFTER = timedelta(minutes=30)
# Bounded per tick so a backlog never pins a worker on an unbounded fan-out of platform reads.
WATCH_SWEEP_LIMIT = 200

FIX_RUN_PROMPT = (
    "CI failed on this merge request. Failed jobs: {names}. Pipeline: {url}\n\n"
    "Read the job logs to find the cause, then fix it. If the failure is not something a "
    "code change can fix, explain why instead of guessing."
)


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


def failed_job_names(pipeline: Pipeline, *, default: str = "") -> str:
    """Comma-separated names of the jobs whose failure counts, or ``default`` when none are visible."""
    return ", ".join(job.name for job in failed_jobs(pipeline)) or default


def judge_pipeline(pipeline: Pipeline | None) -> Judgment:
    """Decide whether a pipeline is green, worth an agent run, or not ours to judge.

    Deliberately conservative: anything that is not an unambiguous pass or an unambiguous
    failure is ``UNCLEAR``, which stops the watch instead of spending an attempt.
    """
    if pipeline is None or pipeline.status not in VERDICT_STATUSES or not pipeline.jobs:
        return Judgment.UNCLEAR
    if pipeline.status == "success":
        return Judgment.GREEN
    # A failure with no failed job visible at all (e.g. a config error) is still worth a look.
    failing = [job for job in pipeline.jobs if job.is_failed()]
    if failing and all(job.allow_failure for job in failing):
        return Judgment.GREEN
    return Judgment.ACTIONABLE


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

    Create-then-enqueue is not cosmetic: ``_ais_fix_run`` below reads ``trigger_type`` back off
    the Run row, which is what keeps a re-arming fix run from resetting ``watch_attempts``.
    """
    from jobs.tasks import run_job_task

    from sessions.services import acreate_run, amark_failed_and_advance

    # Untranslated: this is addressed to the model, not the user. A filled catalog would
    # otherwise hand the agent its instructions in the operator's language.
    prompt = FIX_RUN_PROMPT.format(names=failed_job_names(pipeline, default="the pipeline"), url=pipeline.web_url)

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
    try:
        result = await run_job_task.aenqueue(
            repo_id=repo_id,
            prompt=prompt,
            thread_id=session.thread_id,
            ref=session.ref,
            sandbox_environment_id=sandbox_environment_id,
            run_id=str(run.pk),
            user_id=session.user_id,
        )
    except Exception as err:  # noqa: BLE001
        # The claim already charged the attempt and moved the row to FIXING, but nothing will
        # run — refund both so the watch stays live instead of waiting out the stale sweep.
        logger.exception("pipeline_watch: enqueue failed for run=%s thread_id=%s", run.pk, session.thread_id)
        await amark_failed_and_advance(
            run, prefix="enqueue_failed", err=err, previous_status=RunStatus.READY, log_context="pipeline_watch"
        )
        await Session.objects.filter(thread_id=session.thread_id, watch_state=WatchState.FIXING).aupdate(
            watch_state=WatchState.WATCHING, watch_attempts=F("watch_attempts") - 1, watch_pipeline_id=None
        )
        return
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

    names = failed_job_names(pipeline, default=_("the pipeline"))
    try:
        await sync_to_async(notify)(
            recipient=user,
            event_type=EventType.PIPELINE_WATCH_EXHAUSTED,
            source_type="session",
            source_id=session.thread_id,
            subject=_("CI still failing on {repo}!{iid}").format(repo=session.repo_id, iid=session.merge_request_iid),
            body=_("I stopped after {attempts} attempts and CI is still failing: {names}.").format(
                attempts=session.watch_attempts, names=names
            ),
            link_url=pipeline.web_url,
            channels=[cls.channel_type for cls in enabled_channels()],
        )
    except Exception:
        logger.exception("pipeline_watch: failed to notify %s", user.pk)


async def aarm_watch(
    *, repo_id: str, merge_request_iid: int, ref: str, was_fix_run: bool, user_id: int | None = None
) -> str | None:
    """Point the watch at a merge request DAIV just published.

    Returns the MR thread id it armed, or ``None`` when the repo has the feature off. The
    counter survives re-arming by a fix run — that is what bounds the loop.

    ``user_id`` is the publishing run's user, attributed here because this call is what
    creates the MR thread's session; a row created without one leaves fix runs unattributed.
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
            "user_id": user_id,
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


async def _ais_fix_run(run_id: str | None) -> bool:
    """Whether this run was dispatched by the watch, which is what stops the arm resetting
    ``watch_attempts``. A run with no row reads as False, so every fix-run dispatcher must pass
    its ``run_id`` through to here or the loop bound is lost.
    """
    if not run_id:
        return False
    trigger_type = await Run.objects.filter(pk=run_id).values_list("trigger_type", flat=True).afirst()
    return trigger_type == SessionOrigin.PIPELINE_WEBHOOK


async def aarm_watch_after_run(
    *,
    repo_id: str,
    merge_request: MergeRequest | dict | None,
    published: bool,
    run_id: str | None = None,
    user_id: int | None = None,
) -> None:
    """Point the CI watch at the merge request a finished run published, then evaluate at once.

    Every seam that finishes a publishing run calls this — the job runner, the issue addressor
    and the chat stream — so the watch does not depend on which one ran. The immediate
    evaluation is load-bearing: the push happened during publish, so the pipeline event it
    triggered can arrive before this watch exists.

    ``published`` must be the publisher's own verdict, never ``code_changes``: that stays true
    for a clean tree already on its merge request, which every fix run is by definition.
    """
    from sessions.tasks import evaluate_pipeline_watch_task

    payload = mr_to_payload(merge_request) or {}
    merge_request_iid = payload.get("id")
    ref = payload.get("source_branch")
    if not merge_request_iid or not ref:
        return

    was_fix_run = await _ais_fix_run(run_id)

    if not published:
        # A fix run that pushed nothing will get no pipeline and therefore no event, so
        # re-arming would strand the watch until it aged out.
        if was_fix_run:
            await aexhaust_watch(
                repo_id=repo_id,
                merge_request_iid=merge_request_iid,
                reason=_("I could not find a change that would fix it"),
            )
        return

    armed = await aarm_watch(
        repo_id=repo_id, merge_request_iid=merge_request_iid, ref=ref, was_fix_run=was_fix_run, user_id=user_id
    )
    if armed is None:
        return
    await evaluate_pipeline_watch_task.aenqueue(repo_id=repo_id, ref=ref)


async def _apolled_pipeline_is_current(*, session: Session, pipeline: Pipeline, repo_id: str) -> bool:
    """Whether a *polled* pipeline belongs to the merge request's current head commit.

    ``get_latest_pipeline_for_ref`` correlates with nothing, so without this a pipeline from an
    earlier push is judged. An unreadable head sha means "cannot correlate", so it skips.
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


async def _aclose_watch(session: Session, state: WatchState, *, pipeline_id: int) -> bool:
    """Move a watch out of WATCHING into a terminal state, and say whether we own the move.

    Same race as the dispatch claim below: two events finishing together both read WATCHING,
    and only the row count says which one may comment and notify.
    """
    closed = await Session.objects.filter(thread_id=session.thread_id, watch_state=WatchState.WATCHING).aupdate(
        watch_state=state, watch_pipeline_id=pipeline_id
    )
    return closed == 1


async def arequest_watch_evaluation(*, repo_id: str, ref: str, pipeline_id: int) -> bool:
    """Queue an evaluation for a branch, but only if a watch is actually waiting on it.

    Every platform's CI webhook lands here. The existence check is what makes that affordable:
    CI fires on every branch of every repo and only a DAIV-published MR branch has a watch, so
    without it each unrelated pipeline buys an interactive-queue round-trip to learn there is
    nothing to do. Returns whether it enqueued.
    """
    from sessions.tasks import evaluate_pipeline_watch_task

    if not await Session.objects.filter(repo_id=repo_id, ref=ref, watch_state=WatchState.WATCHING).aexists():
        return False
    await evaluate_pipeline_watch_task.aenqueue(repo_id=repo_id, ref=ref, pipeline_id=pipeline_id)
    return True


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
        await _aclose_watch(session, WatchState.GREEN, pipeline_id=pipeline.id)
        return

    if judgment is Judgment.UNCLEAR:
        if not await _aclose_watch(session, WatchState.UNCLEAR, pipeline_id=pipeline.id):
            return
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
    max_attempts = watch_max_attempts(config)
    if session.watch_attempts >= max_attempts:
        if not await _aclose_watch(session, WatchState.EXHAUSTED, pipeline_id=pipeline.id):
            return
        if merge_request_iid:
            names = failed_job_names(pipeline, default=_("the pipeline"))
            await _apost_watch_note(
                repo_id=repo_id,
                merge_request_iid=merge_request_iid,
                body=_("I stopped after {attempts} attempts and CI is still failing: {names}. Pipeline: {url}").format(
                    attempts=session.watch_attempts, names=names, url=pipeline.web_url
                ),
            )
        await anotify_watch_exhausted(session=session, pipeline=pipeline)
        return

    # Compare-and-swap out of WATCHING: only the row count says which of two concurrent readers
    # owns the attempt. The cap is re-asserted because a stale read can outlive a fix-run cycle.
    claimed = await Session.objects.filter(
        thread_id=session.thread_id, watch_state=WatchState.WATCHING, watch_attempts__lt=max_attempts
    ).aupdate(
        watch_state=WatchState.FIXING,
        watch_attempts=F("watch_attempts") + 1,
        watch_pipeline_id=pipeline.id,
        watch_armed_at=timezone.now(),
    )
    if claimed != 1:
        return
    await _adispatch_fix_run(session=session, pipeline=pipeline, repo_id=repo_id, merge_request_iid=merge_request_iid)


async def areconcile_watches() -> int:
    """Repair watches that events did not resolve.

    Three jobs, and the order is load-bearing: expiring first is what bounds the two sweeps
    below to a live watch, so neither needs an age floor of its own. Returns rows updated by the
    two bulk sweeps plus branches re-judged by the third, which is a progress signal, not a count
    of rows written.
    """
    now = timezone.now()

    touched = await Session.objects.filter(
        watch_state__in=WatchState.open(), watch_armed_at__lt=now - WATCH_MAX_AGE
    ).aupdate(watch_state=WatchState.UNCLEAR)

    # A fix run that stopped heartbeating is dead by the same definition SessionLock uses to
    # take its slot over; the two have to move together.
    touched += await Session.objects.filter(
        watch_state=WatchState.FIXING, watch_armed_at__lt=stale_cutoff(now)
    ).aupdate(watch_state=WatchState.WATCHING, watch_armed_at=now)

    stale = Session.objects.filter(
        watch_state=WatchState.WATCHING, watch_armed_at__lt=now - WATCH_STALE_AFTER
    ).values_list("repo_id", "ref", "thread_id")[:WATCH_SWEEP_LIMIT]
    async for repo_id, ref, thread_id in stale:
        try:
            await aevaluate_watch(repo_id=repo_id, ref=ref, pipeline_id=None)
        except Exception:
            logger.exception("pipeline_watch: reconcile failed for thread_id=%s", thread_id)
        touched += 1

    return touched
