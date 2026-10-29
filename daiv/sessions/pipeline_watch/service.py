"""The orchestrator: judge one branch's pipeline and take the single action it implies.

Owns no I/O and no SQL of its own — ``platform`` reads CI, ``store`` reads and writes the watch
columns, ``policy`` says what the repository is allowed to do, and ``judgment`` says what a
pipeline means. What lives here is the sequencing between them.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext as _

from sessions.models import WatchState
from sessions.pipeline_watch.dispatch import FixRunDispatcher
from sessions.pipeline_watch.judgment import Judgment, PipelineReport
from sessions.pipeline_watch.platform import WatchPlatform, log_read_failure
from sessions.pipeline_watch.policy import WatchPolicy
from sessions.pipeline_watch.store import WatchStore, watch_thread_id
from sessions.repo_state import mr_to_payload
from sessions.signal_defs import watch_exhausted

if TYPE_CHECKING:
    from codebase.base import MergeRequest
    from sessions.models import Session

logger = logging.getLogger("daiv.sessions")


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
        dispatcher: FixRunDispatcher | None = None,
        policy: WatchPolicy | None = None,
        store: WatchStore | None = None,
    ) -> None:
        self.repo_id = repo_id
        self._platform = platform or WatchPlatform(repo_id)
        self._store = store or WatchStore()
        self._dispatcher = dispatcher or FixRunDispatcher(self._store)
        self._policy = policy

    async def _apolicy(self) -> WatchPolicy:
        if self._policy is None:
            self._policy = await WatchPolicy.afor_repo(self.repo_id)
        return self._policy

    def _thread_id(self, merge_request_iid: int) -> str:
        return watch_thread_id(self.repo_id, merge_request_iid)

    async def astate(self, merge_request_iid: int) -> str | None:
        """The state of the watch on the merge request, or ``None`` while nothing ever armed one."""
        return await self._store.astate(self._thread_id(merge_request_iid))

    async def aarm(
        self,
        *,
        merge_request_iid: int,
        ref: str,
        was_fix_run: bool,
        user_id: int | None = None,
        sandbox_environment_id: str | None = None,
    ) -> str | None:
        """Point the watch at a merge request DAIV just published.

        Returns the MR thread id it armed, or ``None`` when the repo has the feature off. The
        counter survives re-arming by a fix run — that is what bounds the loop.

        ``user_id`` is the originating run's owner. This method is what *creates* the MR thread on
        almost every path, so without it that session is ownerless: the give-up notification has no
        recipient and the fix run gets no personal MCP servers. An existing owner is never
        reassigned — the thread may be a human's MR conversation.

        ``sandbox_environment_id`` is the originating run's environment, which the fix run reuses. Like the
        owner, it is adopted only by a thread that has none, and only if the thread's owner can see it.
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
            sandbox_environment_id=sandbox_environment_id,
            reset_attempts=not was_fix_run,
        )
        return thread_id

    async def aturn_on(
        self, *, merge_request_iid: int, user_id: int | None = None, sandbox_environment_id: str | None = None
    ) -> str | None:
        """Watch a merge request because someone asked to, then judge its head pipeline at once.

        Arms on the merge request's current source branch, read from the platform rather than a session's ``ref``,
        which lags a branch the agent renamed. Attempts start over: a person asking again is a new budget, but a
        watch still open is left as it is. Returns the watch's thread id, or ``None`` when the repository has the
        watch off. Raises when the merge request cannot be read.
        """
        from sessions.tasks import evaluate_pipeline_watch_task

        if await self.astate(merge_request_iid) in WatchState.open():
            return self._thread_id(merge_request_iid)
        if not (await self._apolicy()).enabled:
            return None
        merge_request = await self._platform.aread_merge_request(merge_request_iid)
        armed = await self.aarm(
            merge_request_iid=merge_request_iid,
            ref=merge_request.source_branch,
            was_fix_run=False,
            user_id=user_id,
            sandbox_environment_id=sandbox_environment_id,
        )
        await evaluate_pipeline_watch_task.aenqueue(repo_id=self.repo_id, ref=merge_request.source_branch)
        return armed

    async def aturn_off(self, *, merge_request_iid: int, seen_state: str) -> bool:
        """Stop watching because someone asked to, reporting whether the watch is off now.

        ``seen_state`` is the state the person turned off. The move is conditional on it, so one that lost to a
        fix run claiming the watch, or to a verdict landing, changes nothing and the caller shows them the new state.
        """
        if seen_state == WatchState.OFF:
            return True
        return await self._store.atransition(
            self._thread_id(merge_request_iid), expect=seen_state, watch_state=WatchState.OFF
        )

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
        sandbox_environment_id: str | None = None,
        verification_failed: bool = False,
    ) -> None:
        """Point the CI watch at the merge request a finished run published, then evaluate at once.

        Every trigger that arms reaches this through the run executor's ``arm_watch`` (jobs, the issue
        addressor and chat), so the watch does not depend on which one ran. The immediate
        evaluation is load-bearing: the push happened during publish, so the pipeline event it
        triggered can arrive before this watch exists. A run whose ``before_publish`` checks still
        fail ends any open watch instead of arming one; the executor sends such a run here even
        when it does not arm.

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
        if was_fix_run and await self.astate(merge_request_iid) == WatchState.OFF:
            # Someone turned the watch off while this fix run worked; its re-arm must not turn it back on.
            return

        if not published:
            # A fix run that pushed nothing will get no pipeline and therefore no event, so
            # re-arming would strand the watch until it aged out.
            if was_fix_run:
                await self.aexhaust(
                    merge_request_iid=merge_request_iid, reason=_("I could not find a change that would fix it")
                )
            return

        if verification_failed:
            # The agent already spent its fix rounds on these failures, so a person looks before any CI fix run.
            await self.aexhaust(
                merge_request_iid=merge_request_iid,
                reason=_("the before_publish checks still fail after the fix attempt")
                if was_fix_run
                else _("the before_publish checks still fail"),
            )
            return

        armed = await self.aarm(
            merge_request_iid=merge_request_iid,
            ref=ref,
            was_fix_run=was_fix_run,
            user_id=user_id,
            sandbox_environment_id=sandbox_environment_id,
        )
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

        await self._platform.aensure_client()
        try:
            pipeline = await self._platform.aread_pipeline(ref=ref, pipeline_id=pipeline_id)
        except Exception as exc:
            # Unreadable is not a verdict: leaving the watch armed lets the next event or sweep retry.
            log_read_failure(exc, what="the pipeline", target=f"{self.repo_id}@{ref}")
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

        if await self._store.aclaim_attempt(session.thread_id, max_attempts=max_attempts, pipeline_id=report.id):
            await self._dispatcher.adispatch(
                session=session, report=report, repo_id=self.repo_id, merge_request_iid=merge_request_iid
            )

    async def _anotify_exhausted(self, *, session: Session, report: PipelineReport) -> None:
        """Tell the receivers the watch gave up.

        Best-effort: ``asend_robust`` logs a failing receiver, and the MR comment is the reliable channel.
        """
        await watch_exhausted.asend_robust(
            sender=type(session),
            session=session,
            failing_jobs=[job.name for job in report.failed_jobs],
            pipeline_url=report.web_url,
            pipeline_id=report.id,
        )
