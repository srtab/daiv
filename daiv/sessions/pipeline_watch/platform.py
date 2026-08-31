"""The only git-platform I/O in the package."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from codebase.clients import RepoClient
from codebase.clients.base import is_transient_platform_error

if TYPE_CHECKING:
    from codebase.base import Pipeline
    from sessions.pipeline_watch.judgment import PipelineReport

logger = logging.getLogger("daiv.sessions")


def log_read_failure(exc: Exception, *, what: str, target: str) -> None:
    """Log a failed platform read at the level its transience deserves.

    Transient gets WARNING with no traceback — these paths run on every webhook and every sweep, so
    an outage would otherwise mint a Sentry error per event; anything else keeps its traceback.
    """
    if is_transient_platform_error(exc):
        logger.warning("pipeline_watch: could not read %s for %s: %s", what, target, exc)
    else:
        # ``exc_info=exc`` rather than ``logger.exception``: this runs outside the handler.
        logger.error("pipeline_watch: unexpected failure reading %s for %s", what, target, exc_info=exc)


class WatchPlatform:
    """One repository's CI reads and merge-request notes, over one lazily-created client.

    Every method resolves the client through ``_aclient`` rather than trusting a caller to
    pre-build it: ``RepoClient.create_instance`` is process-cached but calls the GitHub API on the
    first build, and ``sync_to_async(self.client.get_pipeline)`` would run that on the event loop.
    What the per-instance memo then saves is the thread hop, not the installation lookup.

    The three methods have deliberately different error policies. ``aread_pipeline`` raises,
    because an unreadable pipeline is not a verdict and only the caller knows whether to leave the
    watch armed. ``ais_head_pipeline`` answers ``False``, because "cannot correlate" is a skip.
    ``apost_note`` swallows, because the note is best-effort — including a client it cannot build,
    since both callers have already committed the transition the note only announces.
    """

    def __init__(self, repo_id: str, client: RepoClient | None = None) -> None:
        self.repo_id = repo_id
        self._client = client

    async def _aclient(self) -> RepoClient:
        if self._client is None:
            self._client = await sync_to_async(RepoClient.create_instance)()
        return self._client

    async def aensure_client(self) -> None:
        """Build the client outside the caller's read guard, raising if it cannot be built.

        A client we cannot build is a deployment fault, not a CI outage, and
        ``is_transient_platform_error`` counts the 401/403 a dead installation returns as
        transient — so a build first attempted inside that guard would log a permanent
        misconfiguration at WARNING, which reaches no one, on every webhook and every sweep.
        """
        await self._aclient()

    async def aread_pipeline(self, *, ref: str, pipeline_id: int | None) -> Pipeline | None:
        client = await self._aclient()
        if pipeline_id is not None:
            return await sync_to_async(client.get_pipeline)(self.repo_id, pipeline_id)
        return await sync_to_async(client.get_latest_pipeline_for_ref)(self.repo_id, ref)

    async def ais_head_pipeline(self, *, merge_request_iid: int | None, report: PipelineReport) -> bool:
        """Whether a pipeline belongs to the merge request's current head commit.

        A webhook names *which* pipeline it reports, not *whether* that pipeline is still the head —
        GitLab auto-cancels redundant pipelines, so the push after ours makes the older one emit a
        terminal ``canceled``. An unreadable head sha means "cannot correlate", which is a skip.
        """
        if not merge_request_iid:
            return False
        client = await self._aclient()
        try:
            merge_request = await sync_to_async(client.get_merge_request)(self.repo_id, merge_request_iid)
        except Exception as exc:
            log_read_failure(exc, what="head sha", target=f"{self.repo_id}!{merge_request_iid}")
            return False
        if merge_request is None:
            logger.error("pipeline_watch: get_merge_request returned None for %s!%s", self.repo_id, merge_request_iid)
            return False
        return bool(merge_request.sha) and report.sha == merge_request.sha

    async def apost_note(self, *, merge_request_iid: int | None, body: str) -> None:
        if not merge_request_iid:
            return
        try:
            client = await self._aclient()
            await sync_to_async(client.create_merge_request_comment)(self.repo_id, merge_request_iid, body)
        except Exception:
            logger.exception("pipeline_watch: failed to comment on %s!%s", self.repo_id, merge_request_iid)
