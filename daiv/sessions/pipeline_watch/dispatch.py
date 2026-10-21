"""Dispatching a fix run, and refunding the claim when the enqueue does not land."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from automation.agent import validators
from sessions.models import RunStatus, SessionOrigin
from sessions.pipeline_watch.store import WatchStore
from sessions.queue import aenqueue_run
from sessions.services import acreate_run

if TYPE_CHECKING:
    from sessions.models import Session
    from sessions.pipeline_watch.judgment import PipelineReport

logger = logging.getLogger("daiv.sessions")

FIX_RUN_PROMPT = (
    "CI failed on this merge request. Failed jobs: {names}. Pipeline: {url}\n\n"
    "Read the job logs to find the cause, then fix it. If the failure is not something a "
    "code change can fix, explain why instead of guessing."
)


def _session_agent(session: Session) -> tuple[str, str]:
    """The thread's pinned model and thinking level; empty when it pins none or its model cannot be used."""
    try:
        return validators.validate_agent_override(session.agent_model, session.agent_thinking_level)
    except validators.AgentOverrideError as err:
        logger.warning(
            "pipeline_watch: thread_id=%s pins model %r, which cannot be used (%s); the fix run uses the default",
            session.thread_id,
            session.agent_model,
            err,
        )
        return "", ""


class FixRunDispatcher:
    def __init__(self, store: WatchStore | None = None) -> None:
        self._store = store or WatchStore()

    async def adispatch(
        self, *, session: Session, report: PipelineReport, repo_id: str, merge_request_iid: int
    ) -> None:
        """Create the fix Run, then enqueue the agent task against it.

        Create-then-enqueue is not cosmetic: ``WatchStore.ais_fix_run`` reads ``trigger_type`` back
        off the Run row, which is what keeps a re-arming fix run from resetting ``watch_attempts``.
        The fix run reuses the session's recorded env: it redoes that session's work, and re-matching would lose an
        explicit pick.
        """
        # Untranslated: this is addressed to the model, not the user. A filled catalog would
        # otherwise hand the agent its instructions in the operator's language.
        prompt = FIX_RUN_PROMPT.format(names=report.failed_job_names(default="the pipeline"), url=report.web_url)

        user = await sync_to_async(lambda: session.user)()
        sandbox_environment_id = str(session.sandbox_environment_id) if session.sandbox_environment_id else None
        agent_model, agent_thinking_level = _session_agent(session)

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
            agent_model=agent_model,
            agent_thinking_level=agent_thinking_level,
            status=RunStatus.READY,
        )
        failure = await aenqueue_run(run, log_context="pipeline_watch")
        if failure is not None and not failure.task_enqueued:
            # The claim already charged the attempt and moved the watch to FIXING, but no task will run: refund both
            # so the watch stays live instead of waiting out the stale sweep.
            await self._store.arefund_attempt(session.thread_id)
