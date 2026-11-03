"""The orchestrator: judge one branch's pipeline and post what it found to the owning session's inbox.

Owns no I/O and no SQL of its own — ``platform`` reads CI, ``store`` reads and writes the watch columns, ``policy``
says what the repository is allowed to do, ``judgment`` says what a pipeline means, and ``sessions.inbox`` delivers.
What lives here is the sequencing between them, and the wake rule.

A head pipeline posts ``ci_passed`` (green), ``ci_stopped`` (a status that needs a person) or ``ci_failed``. A
failure wakes the owning session only when all of these hold, and the first that fails is the event's ``reason``:

1. the watch is on for the merge request and the repository (``watch_off``);
2. the pipeline ran on the commit DAIV last pushed (``not_daiv_commit``); a blank one, left by a migrated watch,
   counts as DAIV's;
3. no CI-woken run examined that commit yet (``already_examined``);
4. attempts are left (``budget_spent``);
5. the consecutive-run limit is not reached (``run_limit``).

The drain charges the attempt and records the examined commit when it starts the run. A run that pushes nothing
leaves the watch observing: no pipeline follows, and the run's summary is the explanation. ``budget_spent`` records
the attempts spent and tells the owner the watch gave up on DAIV's commit, once per commit however often its pipeline
is retried. Turning the watch back on gives it its attempts back and judges the latest pipeline of each CI subject on
the branch again at once. A failure the watch kept quiet is keyed apart from one that wakes, so judging the same
pipeline once the watch is back on still wakes.

Each result is posted under its CI subject (``kinds.ci_subject``): the merge request's on GitLab, whose one pipeline
covers a commit, and its workflow's on GitHub, which runs one workflow run per workflow, so one workflow's newer run
never replaces another's failure. A pipeline is judged once per status: a retried one keeps its id, so its new result
is judged and posted under a key of its own. One older than a pipeline already judged on the same commit and subject
was replaced, so a late delivery is skipped. A GitLab child pipeline is never judged: it runs on its parent's ref and
commit, and the merge request's result is its parent's.

A claim, a watch turned back on and the reconciler judge the latest pipeline of each CI subject on a branch
(:meth:`PipelineWatch.aevaluate_latest`). The merge request remembers only the last pipeline it judged, so there one
whose result in that status was already posted for its subject is skipped, unless the watch forgot the last pipeline
judged to have it judged anew, or the result was kept quiet because the watch was off and the watch is on now.

When the owning session was delegated, its parent remembers a ``budget_spent`` failure, and a pass once the latest
pipeline of every CI subject on the branch passed on the commit, as ``delegated_ci_outcome``;
:mod:`sessions.inbox.complete` adds a CI-woken run that pushed nothing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sessions.inbox import kinds
from sessions.inbox.ownership import aopen_ownership
from sessions.inbox.policy import WakePolicy
from sessions.inbox.post import apost_delegated_ci_outcome, apost_event
from sessions.models import EventKind, QuietReason
from sessions.pipeline_watch.judgment import Judgment, PipelineReport
from sessions.pipeline_watch.platform import WatchPlatform, log_read_failure
from sessions.pipeline_watch.policy import WatchPolicy
from sessions.pipeline_watch.store import WatchStore
from sessions.signal_defs import watch_exhausted

if TYPE_CHECKING:
    from codebase.base import Pipeline
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
        """Watch the merge request again because someone asked to, with its attempts back, then judge the latest
        pipeline of each CI subject on the branch at once, so a failure kept quiet while the watch was off or out of
        attempts wakes the session. A watch already on is left as it is. Returns ``False`` when the repository has the
        watch off."""
        from sessions.tasks import evaluate_pipeline_watch_task

        if not (await self._apolicy()).enabled:
            return False
        if ownership.watch_enabled:
            return True
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

    async def aevaluate(self, *, ref: str, pipeline_id: int) -> None:
        """Judge a pipeline of an owned branch and post the result to the owning session. A failed post forgets the
        pipeline was judged, so the next event or the reconciler retries."""
        ownership = await self._store.aopen(self.repo_id, ref)
        if ownership is None:
            return

        await self._platform.aensure_client()
        try:
            pipeline = await self._platform.aread_pipeline(pipeline_id=pipeline_id)
        except Exception as exc:
            # Unreadable is not a verdict: the next event or the reconciler retries.
            log_read_failure(exc, what="the pipeline", target=f"{self.repo_id}@{ref}")
            return
        await self._ajudge(ownership, ref, pipeline)

    async def aevaluate_latest(self, *, ref: str) -> bool:
        """Judge the latest pipeline of each CI subject on an owned branch, each as :meth:`aevaluate` judges one, and
        return whether every one was read. One already judged in its status is skipped before its jobs are read
        (:meth:`WatchStore.ajudged`), unless the watch forgot the last pipeline it judged. A subject that fails to be
        judged does not stop the others; the first such error is raised once all were tried."""
        ownership = await self._store.aopen(self.repo_id, ref)
        if ownership is None:
            return True

        await self._platform.aensure_client()
        try:
            listed = await self._platform.aread_latest_pipelines(ref=ref)
        except Exception as exc:
            log_read_failure(exc, what="the latest pipelines", target=f"{self.repo_id}@{ref}")
            return False
        watch_on = ownership.watch_enabled and (await self._apolicy()).enabled
        read, errors = True, []
        for latest in listed:
            subject = kinds.Subject.ci(ownership.merge_request_iid, latest.workflow_id)
            if ownership.last_pipeline_id is not None and await self._store.ajudged(
                ownership.session_id, subject, latest.id, latest.status, watch_on=watch_on
            ):
                continue
            try:
                pipeline = await self._platform.aread_pipeline(pipeline_id=latest.id)
            except Exception as exc:
                log_read_failure(exc, what="the pipeline", target=f"{self.repo_id}@{ref}")
                read = False
                continue
            try:
                await self._ajudge(ownership, ref, pipeline)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]
        return read

    async def _ajudge(self, ownership: MergeRequestOwnership, ref: str, pipeline: Pipeline | None) -> None:
        report = PipelineReport.of(pipeline)
        if report is None or not report.is_judgeable:
            logger.debug(
                "pipeline_watch: nothing to judge yet on %s@%s (status=%s)",
                self.repo_id,
                ref,
                report.status if report else "missing",
            )
            return
        if (report.id, report.status) == (ownership.last_pipeline_id, ownership.last_pipeline_status):
            return
        if await self._store.asuperseded(ownership.session_id, _subject(ownership, report), report.sha, report.id):
            logger.info(
                "pipeline_watch: pipeline %s on %s@%s was replaced by a newer one", report.id, self.repo_id, ref
            )
            return
        if not await self._platform.ais_head_pipeline(merge_request_iid=ownership.merge_request_iid, report=report):
            logger.info("pipeline_watch: pipeline %s is not the head of %s@%s", report.id, self.repo_id, ref)
            return
        if not await self._store.arecord_pipeline(ownership.pk, report.id, report.status):
            return
        try:
            if (owned := await aopen_ownership(self.repo_id, ownership.merge_request_iid)) is not None:
                await self._apost(ownership=owned, session=owned.session, report=report)
        except Exception:
            previous = (ownership.last_pipeline_id, ownership.last_pipeline_status)
            await self._store.aforget_pipeline(ownership.pk, report.id, report.status, previous=previous)
            raise

    async def _apost(self, *, ownership: MergeRequestOwnership, session: Session, report: PipelineReport) -> None:
        reason: QuietReason | None = None
        if report.judgment is Judgment.GREEN:
            kind = EventKind.CI_PASSED
        elif report.judgment is Judgment.UNCLEAR:
            kind = EventKind.CI_STOPPED
        else:
            kind = EventKind.CI_FAILED
            reason = await self._aquiet_reason(ownership, session, report)
        payload = {
            "pipeline_id": report.id,
            "sha": report.sha,
            "status": report.status,
            "web_url": report.web_url,
            "failed_jobs": [job.name for job in report.failed_jobs],
        }
        if report.workflow_id is not None:
            payload["workflow"] = report.workflow_name
        if reason == QuietReason.BUDGET_SPENT:
            payload["attempts"] = ownership.attempts
        result = await apost_event(
            session.thread_id,
            kind=kind,
            source_key=kinds.SourceKey.pipeline(report.id, kind, reason),
            payload=payload,
            wake=kind == EventKind.CI_FAILED and reason is None,
            subject=_subject(ownership, report),
            ref=ownership.source_branch,
            merge_request_iid=ownership.merge_request_iid,
            reason=reason,
        )
        if not result.created:
            return
        if reason == QuietReason.BUDGET_SPENT:
            if await self._store.agave_up_before(result.event):
                return
            await self._anotify_exhausted(ownership=ownership, session=session, report=report)
        elif reason == QuietReason.RUN_LIMIT:
            await WakePolicy(session).areport_run_limit()
        if reason == QuietReason.BUDGET_SPENT or (
            kind == EventKind.CI_PASSED and await self._apassing(ownership, session, report)
        ):
            await apost_delegated_ci_outcome(
                session,
                merge_request_iid=ownership.merge_request_iid,
                pipeline_id=report.id,
                outcome="passing" if kind == EventKind.CI_PASSED else "stopped_after_attempts",
                web_url=report.web_url,
            )

    async def _apassing(self, ownership: MergeRequestOwnership, session: Session, report: PipelineReport) -> bool:
        """Whether the merge request passes on the commit of ``report``, a pass: the latest pipeline of every other CI
        subject on the branch is on that commit and succeeded, or was judged a pass. The platform names the subjects,
        since one still running has posted nothing. Only a delegating parent is told, so a session without one costs no
        read."""
        if session.parent_id is None:
            return False
        try:
            listed = await self._platform.aread_latest_pipelines(ref=ownership.source_branch)
        except Exception as exc:
            log_read_failure(exc, what="the latest pipelines", target=f"{self.repo_id}@{ownership.source_branch}")
            return False
        for latest in listed:
            if latest.id == report.id:
                continue
            if latest.sha != report.sha:
                return False
            subject = kinds.Subject.ci(ownership.merge_request_iid, latest.workflow_id)
            if latest.status != "success" and not await self._store.apassed(
                session.pk, subject, latest.id, latest.status
            ):
                return False
        return True

    async def _aquiet_reason(
        self, ownership: MergeRequestOwnership, session: Session, report: PipelineReport
    ) -> QuietReason | None:
        policy = await self._apolicy()
        if not ownership.watch_enabled or not policy.enabled:
            return QuietReason.WATCH_OFF
        if ownership.last_daiv_sha and report.sha != ownership.last_daiv_sha:
            return QuietReason.NOT_DAIV_COMMIT
        if report.sha == ownership.last_examined_sha:
            return QuietReason.ALREADY_EXAMINED
        if ownership.attempts >= policy.max_attempts:
            return QuietReason.BUDGET_SPENT
        return await WakePolicy(session).amay_wake(EventKind.CI_FAILED)

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


def _subject(ownership: MergeRequestOwnership, report: PipelineReport) -> str:
    return kinds.Subject.ci(ownership.merge_request_iid, report.workflow_id)
