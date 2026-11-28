"""What a session does when the merge request of a submodule it published merges or closes
(``specs/006-submodules-and-lfs/research.md`` R21).

A merge marks each ``SessionSubmodule`` row holding that merge request ``merge_pending``, which counts as live work
(``SessionQuerySet.without_live_work``), so no merge archives its session meanwhile, and enqueues
``sessions.tasks.settle_submodule_merge_task``. The task asks the platform which commit the merge produced and whether
it contains the commit the row's parent records. When it doesn't, the task posts a waking ``submodule_merged`` event,
whose run records the merged commit in the parent. Either way it clears the row's branch, merge request and
``merge_pending``, and settles the session (``MergeArchive.settle``), all in one transaction. When the platform can't
tell, the failure is logged, a notice is remembered in the session, and the row is settled the same way: nothing stays
held. A close without merge clears the rows' branch and merge request.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction

from asgiref.sync import sync_to_async

from codebase.clients import RepoClient
from codebase.clients.base import PLATFORM_ERRORS
from sessions.auto_archive import MergeArchive
from sessions.inbox.kinds import SourceKey
from sessions.inbox.post import Inbox
from sessions.live.notify import asession_changed
from sessions.models import EventKind, SessionSubmodule

if TYPE_CHECKING:
    from django.db.models import QuerySet

logger = logging.getLogger("daiv.sessions")

_CLEARED: dict[str, Any] = {
    "ref": "",
    "merge_request_iid": None,
    "merge_request_web_url": "",
    "merge_request_title": "",
    "merge_request_draft": False,
    "merge_pending": False,
}
UNCHECKED_NOTICE = (
    "{merge_request}, the merge request of the submodule `{path}`, was merged, but DAIV couldn't check whether the "
    "merged commit contains the one this session's repository records for `{path}`, so the recorded commit was left as "
    "it is."
)


class SubmoduleMerges:
    """The submodules of DAIV's sessions whose merge request is ``iid`` of ``repo_id``."""

    def __init__(self, repo_id: str, iid: int) -> None:
        self.repo_id = repo_id
        self.iid = iid

    def _rows(self) -> QuerySet[SessionSubmodule]:
        return SessionSubmodule.objects.filter(repo_id=self.repo_id, merge_request_iid=self.iid)

    async def amark_merged(self) -> int:
        """Mark the rows holding the merged merge request ``merge_pending`` and enqueue their settling; returns how many
        it marked."""
        from sessions.tasks import settle_submodule_merge_task

        marked = await self._rows().aupdate(merge_pending=True)
        if marked:
            await settle_submodule_merge_task.aenqueue(repo_id=self.repo_id, iid=self.iid)
        return marked

    async def aclose(self) -> list[str]:
        """Clear the branch and merge request of the rows holding the merge request, closed without a merge; returns
        their sessions' thread ids."""
        thread_ids = [thread_id async for thread_id in self._rows().values_list("session_id", flat=True)]
        if not thread_ids:
            return []
        await self._rows().aupdate(**_CLEARED)
        for thread_id in thread_ids:
            await asession_changed(thread_id)
        return thread_ids

    async def asettle(self) -> None:
        """Settle each ``merge_pending`` row holding the merged merge request: post the repair its parent needs, or a
        notice when the platform couldn't tell, then clear it and settle its session."""
        rows = [row async for row in self._rows().filter(merge_pending=True)]
        if not rows:
            return
        client = await sync_to_async(RepoClient.create_instance)()
        for row in rows:
            merged_sha = await self._arepair_commit(client, row)
            await sync_to_async(self._settle)(row.pk, merged_sha)

    async def _arepair_commit(self, client: RepoClient, row: SessionSubmodule) -> str | None:
        """The commit the merge produced when it doesn't contain the one ``row``'s parent records; ``""`` when it does,
        or when DAIV recorded none; ``None`` when the platform couldn't tell."""
        try:
            merged_sha = await sync_to_async(client.get_merged_commit)(self.repo_id, self.iid)
            if merged_sha is None:
                logger.error(
                    "%s!%s reads as not merged after its merge, so the commit recorded for the submodule %s of "
                    "thread_id=%s wasn't checked",
                    self.repo_id,
                    self.iid,
                    row.path,
                    row.session_id,
                )
                return None
            if not row.recorded_sha:
                return ""
            contains = await sync_to_async(client.is_ancestor)(self.repo_id, row.recorded_sha, merged_sha)
        except PLATFORM_ERRORS:
            logger.exception(
                "Couldn't check whether the merge of %s!%s contains the commit recorded for the submodule %s of "
                "thread_id=%s",
                self.repo_id,
                self.iid,
                row.path,
                row.session_id,
            )
            return None
        return "" if contains else merged_sha

    def _settle(self, row_pk: int, merged_sha: str | None) -> None:
        """Post ``row_pk``'s repair to ``merged_sha``, or a notice when it is ``None``, clear the row and settle its
        session, unless another settle got there first or its merge request changed meanwhile. SYNC ONLY."""
        with transaction.atomic():
            row = (
                SessionSubmodule.objects
                .select_for_update()
                .filter(pk=row_pk, merge_request_iid=self.iid, merge_pending=True)
                .first()
            )
            if row is None:
                return
            source_key = SourceKey.submodule_merged(self.repo_id, self.iid)
            inbox = Inbox(row.session_id)
            if merged_sha:
                inbox.post(
                    kind=EventKind.SUBMODULE_MERGED,
                    source_key=source_key,
                    payload={
                        "path": row.path,
                        "root_repo_id": row.root_repo_id,
                        "merge_request_iid": self.iid,
                        "recorded_sha": row.recorded_sha,
                        "merged_sha": merged_sha,
                    },
                    wake=True,
                    merge_request_iid=self.iid,
                    repo_id=self.repo_id,
                )
            elif merged_sha is None:
                inbox.post(
                    kind=EventKind.NOTICE,
                    source_key=f"{source_key}:unchecked",
                    payload={
                        "text": UNCHECKED_NOTICE.format(merge_request=f"{self.repo_id}!{self.iid}", path=row.path)
                    },
                    wake=False,
                    merge_request_iid=self.iid,
                    repo_id=self.repo_id,
                )
            SessionSubmodule.objects.filter(pk=row.pk).update(**_CLEARED)
            MergeArchive.settle(row.session_id)
