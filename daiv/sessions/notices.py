from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sessions.models import Run

if TYPE_CHECKING:
    import uuid

    from automation.agent.notices import ModelFallbackNotice

logger = logging.getLogger("daiv.sessions")


class RunNoticeRecorder:
    """The ``RunNotices`` the run executor gives ``create_daiv_agent``: saves notices to ``Run.notices`` of ``run_id``.

    A recorder with no run id stores nothing and logs what it was given.
    """

    def __init__(self, run_id: uuid.UUID | str | None):
        self._run_id = run_id
        self._notices: list[dict[str, str]] = []

    def record(self, notice: ModelFallbackNotice) -> None:
        self._notices.append(notice.as_dict())

    async def aflush(self) -> None:
        if not self._notices:
            return
        if self._run_id is None:
            logger.info("No run bound; run notices not stored: %s", self._notices)
            return
        try:
            await Run.objects.filter(pk=self._run_id).aupdate(notices=list(self._notices))
        except Exception:
            # Safe to continue: each notice was already logged at ERROR when it was reported.
            logger.exception("Failed to save notices on run %s", self._run_id)
