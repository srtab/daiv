import functools
import logging
from typing import TYPE_CHECKING

from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext as _

from chat.repo_state import mr_to_payload
from codebase.base import Scope
from codebase.clients.base import is_transient_platform_error
from codebase.utils import compute_thread_id
from sessions.models import Session, WatchState
from sessions.pipeline_watch.dispatch import FixRunDispatcher
from sessions.pipeline_watch.judgment import Judgment, PipelineReport
from sessions.pipeline_watch.notifier import WatchNotifier
from sessions.pipeline_watch.platform import WatchPlatform
from sessions.pipeline_watch.policy import WatchPolicy
from sessions.pipeline_watch.store import WatchStore

if TYPE_CHECKING:
    from codebase.base import MergeRequest

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
        notifier: WatchNotifier | None = None,
        policy: WatchPolicy | None = None,
        store: WatchStore | None = None,
    ) -> None:
        self.repo_id = repo_id
        self._platform = platform or WatchPlatform(repo_id)
        self._store = store or WatchStore()
        self._dispatcher = dispatcher or FixRunDispatcher(self._store)
        self._notifier = notifier or WatchNotifier()
        self._policy = policy

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
                await self._notifier.anotify_exhausted(session=session, report=report)
            return

        # The cap is re-asserted in the claim itself: a stale read can outlive a fix-run cycle.
        if await claim(
            where={"watch_attempts__lt": max_attempts},
            watch_state=WatchState.FIXING,
            watch_attempts=F("watch_attempts") + 1,
            watch_pipeline_id=report.id,
            watch_armed_at=timezone.now(),
        ):
            await self._dispatcher.adispatch(
                session=session, report=report, repo_id=self.repo_id, merge_request_iid=merge_request_iid
            )
