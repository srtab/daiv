from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.urls import reverse
from django.utils.translation import gettext as _

from notifications.channels.registry import enabled_channel_types
from notifications.policy import notification_source_for_watch
from notifications.run_notifiers import deliver_to_recipients

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sessions.models import Session

logger = logging.getLogger("daiv.notifications")


def _render_payload(session: Session, failing_jobs: Sequence[str], pipeline_url: str) -> tuple[str, str, dict]:
    names = ", ".join(failing_jobs) or _("the pipeline")

    if session.merge_request_iid:
        subject = _("CI still failing on {repo}!{iid}").format(repo=session.repo_id, iid=session.merge_request_iid)
    else:
        subject = _("CI still failing on {repo}").format(repo=session.repo_id)

    body = _("I stopped after {attempts} attempts and CI is still failing: {names}.").format(
        attempts=session.watch_attempts, names=names
    )
    context = {
        "status_tone": "failure",
        "status_label": _("CI still failing"),
        "repo_id": session.repo_id,
        "merge_request_iid": session.merge_request_iid,
        "attempts": session.watch_attempts,
        "failing_jobs": list(failing_jobs),
        # Not link_url: the email channel and the Rocket Chat renderer both run that through
        # build_absolute_url, which would prefix the site domain onto an already-absolute CI URL.
        "pipeline_url": pipeline_url,
    }
    return subject, body, context


def emit_watch_exhausted(*, session: Session, failing_jobs: Sequence[str], pipeline_url: str, pipeline_id: int) -> None:
    """Notify the session owner that the pipeline watch gave up on a merge request.

    The caller only reports that the watch is done; the source key, channels, dedup and payload are
    decided here so this event behaves like every other one. Best-effort by design — the merge
    request comment is the reliable channel.
    """
    recipient = session.user
    if recipient is None:
        # Webhook-origin sessions frequently have no DAIV user behind them.
        logger.warning(
            "pipeline watch: no recipient for exhausted watch on %s (thread_id=%s)", session.repo_id, session.thread_id
        )
        return

    subject, body, context = _render_payload(session, failing_jobs, pipeline_url)

    source_type, source_id, event_type = notification_source_for_watch(session, pipeline_id)

    deliver_to_recipients(
        [recipient],
        source_type=source_type,
        source_id=source_id,
        event_type=event_type,
        subject=subject,
        body=body,
        link_url=reverse("session_detail", kwargs={"thread_id": session.thread_id}),
        channels=enabled_channel_types(),
        context=context,
    )
