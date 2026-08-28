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


class WatchPlatform:
    """One repository's CI reads and merge-request notes, over one lazily-created client.

    One instance per repository: constructing a GitHub client costs a live installation lookup, so
    a read, a correlation and a note must not pay for three.

    The three methods have deliberately different error policies. ``aread_pipeline`` raises,
    because an unreadable pipeline is not a verdict and only the caller knows whether to leave the
    watch armed. ``ais_head_pipeline`` answers ``False``, because "cannot correlate" is a skip.
    ``apost_note`` swallows, because the note is best-effort.
    """

    def __init__(self, repo_id: str, client: RepoClient | None = None) -> None:
        self.repo_id = repo_id
        self._client = client

    @property
    def client(self) -> RepoClient:
        if self._client is None:
            self._client = RepoClient.create_instance()
        return self._client

    async def aread_pipeline(self, *, ref: str, pipeline_id: int | None) -> Pipeline | None:
        if pipeline_id is not None:
            return await sync_to_async(self.client.get_pipeline)(self.repo_id, pipeline_id)
        return await sync_to_async(self.client.get_latest_pipeline_for_ref)(self.repo_id, ref)

    async def ais_head_pipeline(self, *, merge_request_iid: int | None, report: PipelineReport) -> bool:
        """Whether a pipeline belongs to the merge request's current head commit.

        A webhook names *which* pipeline it reports, not *whether* that pipeline is still the head —
        GitLab auto-cancels redundant pipelines, so the push after ours makes the older one emit a
        terminal ``canceled``. An unreadable head sha means "cannot correlate", which is a skip.
        """
        if not merge_request_iid:
            return False
        try:
            merge_request = await sync_to_async(self.client.get_merge_request)(self.repo_id, merge_request_iid)
        except Exception as exc:
            if is_transient_platform_error(exc):
                logger.warning(
                    "pipeline_watch: could not read head sha for %s!%s: %s", self.repo_id, merge_request_iid, exc
                )
            else:
                logger.exception(
                    "pipeline_watch: unexpected failure reading head sha for %s!%s", self.repo_id, merge_request_iid
                )
            return False
        if merge_request is None:
            logger.error("pipeline_watch: get_merge_request returned None for %s!%s", self.repo_id, merge_request_iid)
            return False
        return bool(merge_request.sha) and report.sha == merge_request.sha

    async def apost_note(self, *, merge_request_iid: int | None, body: str) -> None:
        if not merge_request_iid:
            return
        try:
            await sync_to_async(self.client.create_merge_request_comment)(self.repo_id, merge_request_iid, body)
        except Exception:
            logger.exception("pipeline_watch: failed to comment on %s!%s", self.repo_id, merge_request_iid)
