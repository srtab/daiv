import functools
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext as _

from asgiref.sync import sync_to_async
from sandbox_envs.services import resolve_env_for_run

from chat.repo_state import mr_to_payload
from codebase.base import Scope
from codebase.clients import RepoClient
from codebase.clients.base import is_transient_platform_error
from codebase.utils import compute_thread_id
from sessions.locks import stale_cutoff
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState
from sessions.pipeline_watch.judgment import JUDGEABLE_PIPELINE_STATUSES, Judgment, PipelineReport
from sessions.pipeline_watch.policy import WatchPolicy

if TYPE_CHECKING:
    from codebase.base import MergeRequest, Pipeline

logger = logging.getLogger("daiv.sessions")

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


async def _aread_pipeline(*, client, repo_id: str, ref: str, pipeline_id: int | None) -> Pipeline | None:
    if pipeline_id is not None:
        return await sync_to_async(client.get_pipeline)(repo_id, pipeline_id)
    return await sync_to_async(client.get_latest_pipeline_for_ref)(repo_id, ref)


async def _apost_watch_note(*, repo_id: str, merge_request_iid: int | None, body: str, client=None) -> None:
    if not merge_request_iid:
        return
    try:
        client = client or RepoClient.create_instance()
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
    prompt = FIX_RUN_PROMPT.format(
        names=PipelineReport(pipeline).failed_job_names(default="the pipeline"), url=pipeline.web_url
    )

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
    """Report that the watch gave up. Best-effort: the MR comment is the real channel.

    Reading the pipeline is this module's job; the source key, channels, dedup and payload are
    the notifications app's.
    """
    from notifications.watch_notifiers import emit_watch_exhausted

    try:
        await sync_to_async(emit_watch_exhausted)(
            session=session,
            failing_jobs=[job.name for job in PipelineReport(pipeline).failed_jobs],
            pipeline_url=pipeline.web_url,
            pipeline_id=pipeline.id,
        )
    except Exception:
        logger.exception("pipeline_watch: failed to notify the owner of thread_id=%s", session.thread_id)


async def aarm_watch(
    *, repo_id: str, merge_request_iid: int, ref: str, was_fix_run: bool, user_id: int | None = None
) -> str | None:
    """Point the watch at a merge request DAIV just published.

    Returns the MR thread id it armed, or ``None`` when the repo has the feature off. The
    counter survives re-arming by a fix run — that is what bounds the loop.

    ``user_id`` is the originating run's owner. This function is what *creates* the MR thread on
    almost every path, so without it that session is ownerless: the give-up notification has no
    recipient and the fix run gets no personal MCP servers and no USER-tier sandbox env. An
    existing owner is never reassigned — the thread may be a human's MR conversation.
    """
    if not (await WatchPolicy.afor_repo(repo_id)).enabled:
        return None

    thread_id = compute_thread_id(repo_slug=repo_id, scope=Scope.MERGE_REQUEST, entity_iid=merge_request_iid)
    session, _created = await Session.objects.aget_or_create(
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
    if user_id and session.user_id is None:
        updates["user_id"] = user_id
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
    if not await _atransition(thread_id, expect=WatchState.open(), watch_state=WatchState.EXHAUSTED):
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


async def _atransition(thread_id: str, *, expect, where: dict | None = None, **updates) -> bool:
    """Move a watch out of the states it is allowed to leave, reporting whether this caller won.

    Every write to ``watch_state`` goes through here. Two events finishing together both pass their
    checks off their own stale read, and only the row count says which one owns the transition —
    without it each posts its own MR comment, which no constraint dedupes. ``where`` adds the
    conditions a caller needs re-asserted inside the same statement.
    """
    expected = [expect] if isinstance(expect, str) else list(expect)
    updated = await Session.objects.filter(thread_id=thread_id, watch_state__in=expected, **(where or {})).aupdate(
        **updates
    )
    return updated == 1


async def _apipeline_is_current(*, client, session: Session, pipeline: Pipeline, repo_id: str) -> bool:
    """Whether a pipeline belongs to the merge request's current head commit.

    A webhook names *which* pipeline it reports, not *whether* that pipeline is still the head —
    GitLab auto-cancels redundant pipelines, so the push after ours makes the older one emit a
    terminal ``canceled``. An unreadable head sha means "cannot correlate", which is a skip.
    """
    if not session.merge_request_iid:
        return False
    try:
        merge_request = await sync_to_async(client.get_merge_request)(repo_id, session.merge_request_iid)
    except Exception as exc:
        if is_transient_platform_error(exc):
            logger.warning(
                "pipeline_watch: could not read head sha for %s!%s: %s", repo_id, session.merge_request_iid, exc
            )
        else:
            logger.exception(
                "pipeline_watch: unexpected failure reading head sha for %s!%s", repo_id, session.merge_request_iid
            )
        return False
    if merge_request is None:
        logger.error("pipeline_watch: get_merge_request returned None for %s!%s", repo_id, session.merge_request_iid)
        return False
    return bool(merge_request.sha) and pipeline.sha == merge_request.sha


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
        .select_related("user")
        .order_by("-watch_armed_at")
        .afirst()
    )
    if session is None:
        return
    if pipeline_id is not None and session.watch_pipeline_id == pipeline_id:
        return

    # One client for the whole evaluation: constructing a GitHub one costs a live installation
    # lookup, so the read, the correlation and the note would otherwise pay for three.
    client = RepoClient.create_instance()
    try:
        pipeline = await _aread_pipeline(client=client, repo_id=repo_id, ref=ref, pipeline_id=pipeline_id)
    except Exception as exc:
        # Unreadable is not a verdict: leaving the watch armed lets the next event or sweep retry.
        # The level splits on transience so an outage does not mint a Sentry error per sweep.
        if is_transient_platform_error(exc):
            logger.warning("pipeline_watch: could not read the pipeline for %s@%s: %s", repo_id, ref, exc)
        else:
            logger.exception("pipeline_watch: unexpected failure reading the pipeline for %s@%s", repo_id, ref)
        return
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
    if not await _apipeline_is_current(client=client, session=session, pipeline=pipeline, repo_id=repo_id):
        logger.info("pipeline_watch: pipeline %s is not the head of %s@%s", pipeline.id, repo_id, ref)
        return

    judgment = PipelineReport(pipeline).judgment
    merge_request_iid = session.merge_request_iid
    claim = functools.partial(_atransition, session.thread_id, expect=WatchState.WATCHING)

    if judgment is Judgment.GREEN:
        await claim(watch_state=WatchState.GREEN, watch_pipeline_id=pipeline.id)
        return

    if judgment is Judgment.UNCLEAR:
        if await claim(watch_state=WatchState.UNCLEAR, watch_pipeline_id=pipeline.id):
            await _apost_watch_note(
                client=client,
                repo_id=repo_id,
                merge_request_iid=merge_request_iid,
                body=_(
                    "I stopped watching CI on this branch: the pipeline did not finish in a way I "
                    "can act on (status `{status}`). Re-run it or mention me if you want another look."
                ).format(status=pipeline.status),
            )
        return

    max_attempts = (await WatchPolicy.afor_repo(repo_id)).max_attempts
    if session.watch_attempts >= max_attempts:
        if await claim(watch_state=WatchState.EXHAUSTED, watch_pipeline_id=pipeline.id):
            await _apost_watch_note(
                client=client,
                repo_id=repo_id,
                merge_request_iid=merge_request_iid,
                body=_("I stopped after {attempts} attempts and CI is still failing: {names}. Pipeline: {url}").format(
                    attempts=session.watch_attempts,
                    names=PipelineReport(pipeline).failed_job_names(default=_("the pipeline")),
                    url=pipeline.web_url,
                ),
            )
            await anotify_watch_exhausted(session=session, pipeline=pipeline)
        return

    # The cap is re-asserted in the claim itself: a stale read can outlive a fix-run cycle.
    if await claim(
        where={"watch_attempts__lt": max_attempts},
        watch_state=WatchState.FIXING,
        watch_attempts=F("watch_attempts") + 1,
        watch_pipeline_id=pipeline.id,
        watch_armed_at=timezone.now(),
    ):
        await _adispatch_fix_run(
            session=session, pipeline=pipeline, repo_id=repo_id, merge_request_iid=merge_request_iid
        )


async def areconcile_watches() -> int:
    """Repair watches that events did not resolve.

    Three jobs, and the order is load-bearing: expiring first is what bounds the two sweeps
    below to a live watch, so neither needs an age floor of its own. Returns watches expired,
    plus rows the bulk un-stick updated, plus branches re-judged — a progress signal, not a count
    of rows written.
    """
    now = timezone.now()

    touched = 0
    expired = Session.objects.filter(watch_state__in=WatchState.open(), watch_armed_at__lt=now - WATCH_MAX_AGE).only(
        "thread_id", "repo_id", "merge_request_iid", "watch_state"
    )[:WATCH_SWEEP_LIMIT]
    async for session in expired:
        if not await _atransition(session.thread_id, expect=WatchState.open(), watch_state=WatchState.UNCLEAR):
            continue
        # Where every unresolved failure lands — unreadable pipeline, dead fix run, pipeline that
        # never started. Closing it silently made those indistinguishable.
        logger.warning(
            "pipeline_watch: giving up on %s!%s after %s in state %s",
            session.repo_id,
            session.merge_request_iid,
            WATCH_MAX_AGE,
            session.watch_state,
        )
        await _apost_watch_note(
            repo_id=session.repo_id,
            merge_request_iid=session.merge_request_iid,
            body=_(
                "I stopped watching CI on this merge request: no result I could act on arrived "
                "within {hours} hours. Mention me if you want another look."
            ).format(hours=int(WATCH_MAX_AGE.total_seconds() // 3600)),
        )
        touched += 1

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
