"""The orchestrator: judge one branch's pipeline and post what it found to the owning session's inbox.

Owns no I/O and no SQL of its own — ``platform`` reads CI, ``store`` reads and writes the watch columns, ``policy``
says what the repository is allowed to do, ``judgment`` says what a pipeline means, and ``sessions.inbox`` delivers.
What lives here is the sequencing between them, and the wake rule.

A head pipeline posts ``ci_passed`` (green), ``ci_stopped`` (a status that needs a person) or ``ci_failed``. A
failure wakes the owning session only when all of these hold, and the first that fails is the event's ``reason``:

1. the watch is on for the merge request and the repository (``watch_off``);
2. the session is not muted (``muted``);
3. the pipeline ran on the commit DAIV last pushed (``not_daiv_commit``); a blank one, left by a migrated watch,
   counts as DAIV's;
4. no CI-woken run examined that commit yet (``already_examined``);
5. attempts are left (``budget_spent``);
6. the consecutive-run limit is not reached (``run_limit``).

The drain charges the attempt and records the examined commit when it starts the run. A run that pushes nothing
leaves the watch observing: no pipeline follows, and the run's summary is the explanation. ``budget_spent`` on a new
DAIV commit tells the owner the watch gave up on it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sessions.inbox import kinds
from sessions.inbox.ownership import aowning_session
from sessions.inbox.policy import amay_wake, areport_run_limit
from sessions.inbox.post import apost_event
from sessions.models import EventKind, QuietReason
from sessions.pipeline_watch.judgment import Judgment, PipelineReport
from sessions.pipeline_watch.platform import WatchPlatform, log_read_failure
from sessions.pipeline_watch.policy import WatchPolicy
from sessions.pipeline_watch.store import WatchStore
from sessions.signal_defs import watch_exhausted

if TYPE_CHECKING:
    from sessions.models import MergeRequestOwnership, Session

logger = logging.getLogger("daiv.sessions")


class PipelineWatch:
    """The CI watch on one repository's merge requests.

    Collaborators are injected so a test can substitute the platform while the store runs against the real database.
    """

    def __init__(
        self,
        repo_id: str,
        *,
        platform: WatchPlatform | None = None,
        policy: WatchPolicy | None = None,
        store: WatchStore | None = None,
    ) -> None:
        self.repo_id = repo_id
        self._platform = platform or WatchPlatform(repo_id)
        self._store = store or WatchStore()
        self._policy = policy

    async def _apolicy(self) -> WatchPolicy:
        if self._policy is None:
            self._policy = await WatchPolicy.afor_repo(self.repo_id)
        return self._policy

    async def aturn_on(self, ownership: MergeRequestOwnership) -> bool:
        """Watch the merge request again because someone asked to, with its attempts back, then judge its head
        pipeline at once. Returns ``False`` when the repository has the watch off."""
        from sessions.tasks import evaluate_pipeline_watch_task

        if not (await self._apolicy()).enabled:
            return False
        await self._store.aset_enabled(ownership.pk, enabled=True)
        await evaluate_pipeline_watch_task.aenqueue(repo_id=self.repo_id, ref=ownership.source_branch)
        return True

    async def aturn_off(self, ownership: MergeRequestOwnership) -> None:
        """Stop waking the owning session for this merge request's CI; results are still remembered."""
        await self._store.aset_enabled(ownership.pk, enabled=False)

    async def arequest_evaluation(self, *, ref: str, pipeline_id: int) -> bool:
        """Queue an evaluation for a branch, but only if a session owns a merge request on it.

        Every platform's CI webhook lands here, and CI fires on every branch of every repository, so the check is what
        keeps an unrelated pipeline from buying an interactive-queue round-trip. Returns whether it enqueued.
        """
        from sessions.tasks import evaluate_pipeline_watch_task

        if not await self._store.ahas_open(self.repo_id, ref):
            return False
        await evaluate_pipeline_watch_task.aenqueue(repo_id=self.repo_id, ref=ref, pipeline_id=pipeline_id)
        return True

    async def aevaluate(self, *, ref: str, pipeline_id: int | None) -> None:
        """Judge the head pipeline of an owned branch and post the result to the owning session."""
        ownership = await self._store.aopen(self.repo_id, ref)
        if ownership is None:
            return
        if pipeline_id is not None and ownership.last_pipeline_id == pipeline_id:
            return

        await self._platform.aensure_client()
        try:
            pipeline = await self._platform.aread_pipeline(ref=ref, pipeline_id=pipeline_id)
        except Exception as exc:
            # Unreadable is not a verdict: the next event or the reconciler retries.
            log_read_failure(exc, what="the pipeline", target=f"{self.repo_id}@{ref}")
            return

        report = PipelineReport.of(pipeline)
        if report is None or not report.is_judgeable:
            logger.debug(
                "pipeline_watch: nothing to judge yet on %s@%s (status=%s)",
                self.repo_id,
                ref,
                report.status if report else "missing",
            )
            return
        if report.id == ownership.last_pipeline_id:
            return
        if not await self._platform.ais_head_pipeline(merge_request_iid=ownership.merge_request_iid, report=report):
            logger.info("pipeline_watch: pipeline %s is not the head of %s@%s", report.id, self.repo_id, ref)
            return
        if not await self._store.arecord_pipeline(ownership.pk, report.id):
            return
        session = await aowning_session(self.repo_id, ownership.merge_request_iid)
        if session is None:
            return
        await self._apost(ownership=ownership, session=session, report=report)

    async def _apost(self, *, ownership: MergeRequestOwnership, session: Session, report: PipelineReport) -> None:
        reason: QuietReason | None = None
        if report.judgment is Judgment.GREEN:
            kind = EventKind.CI_PASSED
        elif report.judgment is Judgment.UNCLEAR:
            kind = EventKind.CI_STOPPED
        else:
            kind = EventKind.CI_FAILED
            reason = await self._aquiet_reason(ownership, session, report)
        result = await apost_event(
            session.thread_id,
            kind=kind,
            source_key=kinds.pipeline_key(report.id),
            payload={
                "pipeline_id": report.id,
                "sha": report.sha,
                "status": report.status,
                "web_url": report.web_url,
                "failed_jobs": [job.name for job in report.failed_jobs],
            },
            wake=kind == EventKind.CI_FAILED and reason is None,
            subject=kinds.ci_subject(ownership.merge_request_iid),
            ref=ownership.source_branch,
            merge_request_iid=ownership.merge_request_iid,
            reason=reason,
        )
        if result.created and reason == QuietReason.BUDGET_SPENT:
            await self._anotify_exhausted(ownership=ownership, session=session, report=report)
        elif result.created and reason == QuietReason.RUN_LIMIT:
            await areport_run_limit(session)

    async def _aquiet_reason(
        self, ownership: MergeRequestOwnership, session: Session, report: PipelineReport
    ) -> QuietReason | None:
        policy = await self._apolicy()
        if not ownership.watch_enabled or not policy.enabled:
            return QuietReason.WATCH_OFF
        if session.inbox_muted:
            return QuietReason.MUTED
        if ownership.last_daiv_sha and report.sha != ownership.last_daiv_sha:
            return QuietReason.NOT_DAIV_COMMIT
        if report.sha == ownership.last_examined_sha:
            return QuietReason.ALREADY_EXAMINED
        if ownership.attempts >= policy.max_attempts:
            return QuietReason.BUDGET_SPENT
        return await amay_wake(session, EventKind.CI_FAILED)

    async def _anotify_exhausted(
        self, *, ownership: MergeRequestOwnership, session: Session, report: PipelineReport
    ) -> None:
        """Tell the receivers the watch gave up on DAIV's latest commit. Best-effort: ``asend_robust`` logs a failing
        receiver."""
        await watch_exhausted.asend_robust(
            sender=type(ownership),
            ownership=ownership,
            session=session,
            failing_jobs=[job.name for job in report.failed_jobs],
            pipeline_url=report.web_url,
            sha=report.sha,
        )
