"""Dispatching a fix run, and refunding the claim when the enqueue does not land."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from sandbox_envs.services import resolve_env_for_run

from sessions.models import RunStatus, SessionOrigin
from sessions.pipeline_watch.store import WatchStore

if TYPE_CHECKING:
    from sessions.models import Session
    from sessions.pipeline_watch.judgment import PipelineReport

logger = logging.getLogger("daiv.sessions")

FIX_RUN_PROMPT = (
    "CI failed on this merge request. Failed jobs: {names}. Pipeline: {url}\n\n"
    "Read the job logs to find the cause, then fix it. If the failure is not something a "
    "code change can fix, explain why instead of guessing."
)


class FixRunDispatcher:
    def __init__(self, store: WatchStore | None = None) -> None:
        self._store = store or WatchStore()

    async def adispatch(
        self, *, session: Session, report: PipelineReport, repo_id: str, merge_request_iid: int
    ) -> None:
        """Create the fix Run, then enqueue the agent task against it.

        Create-then-enqueue is not cosmetic: ``WatchStore.ais_fix_run`` reads ``trigger_type`` back
        off the Run row, which is what keeps a re-arming fix run from resetting ``watch_attempts``.
        """
        from jobs.tasks import run_job_task

        from sessions.services import acreate_run, amark_failed_and_advance

        # Untranslated: this is addressed to the model, not the user. A filled catalog would
        # otherwise hand the agent its instructions in the operator's language.
        prompt = FIX_RUN_PROMPT.format(names=report.failed_job_names(default="the pipeline"), url=report.web_url)

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
            await self._store.arefund_attempt(session.thread_id)
            return
        try:
            run.task_result_id = result.id
            await run.asave(update_fields=["task_result_id"])
        except Exception:
            logger.exception(
                "pipeline_watch: failed to link task_result_id=%s to run=%s (the fix run still executes)",
                result.id,
                run.pk,
            )
