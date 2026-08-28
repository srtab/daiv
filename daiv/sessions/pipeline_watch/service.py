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
from codebase.clients.base import is_transient_platform_error
from codebase.utils import compute_thread_id
from sessions.locks import stale_cutoff
from sessions.models import RunStatus, Session, SessionOrigin, WatchState
from sessions.pipeline_watch.judgment import Judgment, PipelineReport
from sessions.pipeline_watch.platform import WatchPlatform
from sessions.pipeline_watch.policy import WatchPolicy
from sessions.pipeline_watch.store import WatchStore

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


async def _adispatch_fix_run(*, session: Session, pipeline: Pipeline, repo_id: str, merge_request_iid: int) -> None:
    """Create the fix Run, then enqueue the agent task against it.

    Create-then-enqueue is not cosmetic: ``WatchStore.ais_fix_run`` reads ``trigger_type`` back off
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
        await WatchStore().arefund_attempt(session.thread_id)
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


class PipelineWatch:
    """The CI watch on one repository's merge requests.

    Collaborators are injected so a test can substitute the platform while the store runs against
    the real database. ``repo_id`` binds to the instance because every entry point already carries
    it — that is what lets one client serve a whole evaluation.
    """

    def __init__(
        self,
        repo_id: str,
        *,
        platform: WatchPlatform | None = None,
        dispatcher=None,
        notifier=None,
        policy: WatchPolicy | None = None,
        store: WatchStore | None = None,
    ) -> None:
        self.repo_id = repo_id
        self._platform = platform or WatchPlatform(repo_id)
        self._dispatcher = dispatcher
        self._notifier = notifier
        self._policy = policy
        self._store = store or WatchStore()

    async def _apolicy(self) -> WatchPolicy:
        if self._policy is None:
            self._policy = await WatchPolicy.afor_repo(self.repo_id)
        return self._policy

    def _thread_id(self, merge_request_iid: int) -> str:
        return compute_thread_id(repo_slug=self.repo_id, scope=Scope.MERGE_REQUEST, entity_iid=merge_request_iid)

    async def aarm(
        self, *, merge_request_iid: int, ref: str, was_fix_run: bool, user_id: int | None = None
    ) -> str | None:
        """Point the watch at a merge request DAIV just published.

        Returns the MR thread id it armed, or ``None`` when the repo has the feature off. The
        counter survives re-arming by a fix run — that is what bounds the loop.

        ``user_id`` is the originating run's owner. This method is what *creates* the MR thread on
        almost every path, so without it that session is ownerless: the give-up notification has no
        recipient and the fix run gets no personal MCP servers and no USER-tier sandbox env. An
        existing owner is never reassigned — the thread may be a human's MR conversation.
        """
        if not (await self._apolicy()).enabled:
            return None

        thread_id = self._thread_id(merge_request_iid)
        await self._store.aarm(
            thread_id=thread_id,
            repo_id=self.repo_id,
            ref=ref,
            merge_request_iid=merge_request_iid,
            user_id=user_id,
            reset_attempts=not was_fix_run,
        )
        return thread_id

    async def aexhaust(self, *, merge_request_iid: int, reason: str) -> None:
        """End a watch that cannot make progress, whatever state it is in.

        Used when a fix run produced no diff: no push means no pipeline and therefore no event,
        so nothing else would ever move this watch.
        """
        thread_id = self._thread_id(merge_request_iid)
        if not await self._store.atransition(thread_id, expect=WatchState.open(), watch_state=WatchState.EXHAUSTED):
            return
        await self._platform.apost_note(
            merge_request_iid=merge_request_iid,
            body=_("I stopped watching CI on this merge request: {reason}.").format(reason=reason),
        )

    async def aarm_after_run(
        self,
        *,
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

        was_fix_run = await self._store.ais_fix_run(run_id)

        if not published:
            # A fix run that pushed nothing will get no pipeline and therefore no event, so
            # re-arming would strand the watch until it aged out.
            if was_fix_run:
                await self.aexhaust(
                    merge_request_iid=merge_request_iid, reason=_("I could not find a change that would fix it")
                )
            return

        armed = await self.aarm(merge_request_iid=merge_request_iid, ref=ref, was_fix_run=was_fix_run, user_id=user_id)
        if armed is None:
            return
        await evaluate_pipeline_watch_task.aenqueue(repo_id=self.repo_id, ref=ref)

    async def arequest_evaluation(self, *, ref: str, pipeline_id: int) -> bool:
        """Queue an evaluation for a branch, but only if a watch is actually waiting on it.

        Every platform's CI webhook lands here. The existence check is what makes that affordable:
        CI fires on every branch of every repo and only a DAIV-published MR branch has a watch, so
        without it each unrelated pipeline buys an interactive-queue round-trip to learn there is
        nothing to do. Returns whether it enqueued.
        """
        from sessions.tasks import evaluate_pipeline_watch_task

        if not await self._store.ahas_open_watch(self.repo_id, ref):
            return False
        await evaluate_pipeline_watch_task.aenqueue(repo_id=self.repo_id, ref=ref, pipeline_id=pipeline_id)
        return True

    async def aevaluate(self, *, ref: str, pipeline_id: int | None) -> None:
        """Judge the pipeline for a watched branch and take the one action it implies."""
        session = await self._store.aopen_watch(self.repo_id, ref)
        if session is None:
            return
        if pipeline_id is not None and session.watch_pipeline_id == pipeline_id:
            return

        try:
            pipeline = await self._platform.aread_pipeline(ref=ref, pipeline_id=pipeline_id)
        except Exception as exc:
            # Unreadable is not a verdict: leaving the watch armed lets the next event or sweep retry.
            # The level splits on transience so an outage does not mint a Sentry error per sweep.
            if is_transient_platform_error(exc):
                logger.warning("pipeline_watch: could not read the pipeline for %s@%s: %s", self.repo_id, ref, exc)
            else:
                logger.exception("pipeline_watch: unexpected failure reading the pipeline for %s@%s", self.repo_id, ref)
            return

        report = PipelineReport.of(pipeline)
        if report is None or not report.is_judgeable:
            # Not judgeable *yet* — ``PipelineReport`` reads a still-running pipeline as UNCLEAR, and
            # the arm-time evaluation runs seconds after the push. WATCH_MAX_AGE is the backstop.
            logger.debug(
                "pipeline_watch: nothing to judge yet on %s@%s (status=%s)",
                self.repo_id,
                ref,
                report.status if report else "missing",
            )
            return
        if report.id == session.watch_pipeline_id:
            return
        if not await self._platform.ais_head_pipeline(merge_request_iid=session.merge_request_iid, report=report):
            logger.info("pipeline_watch: pipeline %s is not the head of %s@%s", report.id, self.repo_id, ref)
            return

        await self._aact(session=session, report=report)

    async def _aact(self, *, session: Session, report: PipelineReport) -> None:
        merge_request_iid = session.merge_request_iid
        claim = functools.partial(self._store.atransition, session.thread_id, expect=WatchState.WATCHING)

        if report.judgment is Judgment.GREEN:
            await claim(watch_state=WatchState.GREEN, watch_pipeline_id=report.id)
            return

        if report.judgment is Judgment.UNCLEAR:
            if await claim(watch_state=WatchState.UNCLEAR, watch_pipeline_id=report.id):
                await self._platform.apost_note(
                    merge_request_iid=merge_request_iid,
                    body=_(
                        "I stopped watching CI on this branch: the pipeline did not finish in a way I "
                        "can act on (status `{status}`). Re-run it or mention me if you want another look."
                    ).format(status=report.status),
                )
            return

        max_attempts = (await self._apolicy()).max_attempts
        if session.watch_attempts >= max_attempts:
            if await claim(watch_state=WatchState.EXHAUSTED, watch_pipeline_id=report.id):
                await self._platform.apost_note(
                    merge_request_iid=merge_request_iid,
                    body=_(
                        "I stopped after {attempts} attempts and CI is still failing: {names}. Pipeline: {url}"
                    ).format(
                        attempts=session.watch_attempts,
                        names=report.failed_job_names(default=_("the pipeline")),
                        url=report.web_url,
                    ),
                )
                await self._anotify_exhausted(session=session, report=report)
            return

        # The cap is re-asserted in the claim itself: a stale read can outlive a fix-run cycle.
        if await claim(
            where={"watch_attempts__lt": max_attempts},
            watch_state=WatchState.FIXING,
            watch_attempts=F("watch_attempts") + 1,
            watch_pipeline_id=report.id,
            watch_armed_at=timezone.now(),
        ):
            await self._adispatch(session=session, report=report, merge_request_iid=merge_request_iid)

    async def _anotify_exhausted(self, *, session: Session, report: PipelineReport) -> None:
        if self._notifier is not None:
            await self._notifier.anotify_exhausted(session=session, report=report)
            return
        await anotify_watch_exhausted(session=session, pipeline=report.pipeline)

    async def _adispatch(self, *, session: Session, report: PipelineReport, merge_request_iid: int) -> None:
        if self._dispatcher is not None:
            await self._dispatcher.adispatch(
                session=session, report=report, repo_id=self.repo_id, merge_request_iid=merge_request_iid
            )
            return
        await _adispatch_fix_run(
            session=session, pipeline=report.pipeline, repo_id=self.repo_id, merge_request_iid=merge_request_iid
        )


async def areconcile_watches() -> int:
    """Repair watches that events did not resolve.

    Three jobs, and the order is load-bearing: expiring first is what bounds the two sweeps
    below to a live watch, so neither needs an age floor of its own. Returns watches expired,
    plus rows the bulk un-stick updated, plus branches re-judged — a progress signal, not a count
    of rows written.
    """
    now = timezone.now()
    store = WatchStore()

    touched = 0
    expired = store.expiring_watches(now - WATCH_MAX_AGE, WATCH_SWEEP_LIMIT)
    async for session in expired:
        if not await store.atransition(session.thread_id, expect=WatchState.open(), watch_state=WatchState.UNCLEAR):
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
        await WatchPlatform(session.repo_id).apost_note(
            merge_request_iid=session.merge_request_iid,
            body=_(
                "I stopped watching CI on this merge request: no result I could act on arrived "
                "within {hours} hours. Mention me if you want another look."
            ).format(hours=int(WATCH_MAX_AGE.total_seconds() // 3600)),
        )
        touched += 1

    # A fix run that stopped heartbeating is dead by the same definition SessionLock uses to
    # take its slot over; the two have to move together.
    touched += await store.arecover_stale_fixing(cutoff=stale_cutoff(now), now=now)

    stale = store.stale_watching(now - WATCH_STALE_AFTER, WATCH_SWEEP_LIMIT)
    async for repo_id, ref, thread_id in stale:
        try:
            await PipelineWatch(repo_id).aevaluate(ref=ref, pipeline_id=None)
        except Exception:
            logger.exception("pipeline_watch: reconcile failed for thread_id=%s", thread_id)
        touched += 1

    return touched
