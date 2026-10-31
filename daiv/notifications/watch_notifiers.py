from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.urls import reverse
from django.utils.translation import gettext as _

from asgiref.sync import async_to_sync

from notifications.channels.registry import enabled_channel_types
from notifications.policy import notification_source_for_watch
from notifications.run_notifiers import deliver_to_recipients

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sessions.models import MergeRequestOwnership, Session

logger = logging.getLogger("daiv.notifications")


def _render_payload(
    ownership: MergeRequestOwnership, failing_jobs: Sequence[str], pipeline_url: str
) -> tuple[str, str, dict]:
    names = ", ".join(failing_jobs) or _("the pipeline")
    subject = _("CI still failing on {repo}!{iid}").format(repo=ownership.repo_id, iid=ownership.merge_request_iid)
    body = _("I stopped after {attempts} attempts and CI is still failing: {names}.").format(
        attempts=ownership.attempts, names=names
    )
    context = {
        "status_tone": "failure",
        "status_label": _("CI still failing"),
        "repo_id": ownership.repo_id,
        "merge_request_iid": ownership.merge_request_iid,
        "attempts": ownership.attempts,
        "failing_jobs": list(failing_jobs),
        # Not link_url: the email channel and the chat renderers all run that through
        # build_absolute_url, which would prefix the site domain onto an already-absolute CI URL.
        "pipeline_url": pipeline_url,
    }
    return subject, body, context


def emit_watch_exhausted(
    *, ownership: MergeRequestOwnership, session: Session, failing_jobs: Sequence[str], pipeline_url: str, sha: str
) -> None:
    """Tell the merge request's owner that the CI watch gave up on DAIV's latest commit, linking the owning session.

    The caller only reports that the watch gave up; the source key, channels, dedup and payload are decided here so
    this event behaves like every other one. An ownerless merge request, which webhook-origin sessions often leave,
    gets the outcome as a merge request comment instead.
    """
    from sessions.pipeline_watch.platform import WatchPlatform

    subject, body, context = _render_payload(ownership, failing_jobs, pipeline_url)
    recipient = ownership.owner
    if recipient is None:
        note = f"{body} {_('Pipeline: {url}').format(url=pipeline_url)}"
        async_to_sync(WatchPlatform(ownership.repo_id).apost_note)(
            merge_request_iid=ownership.merge_request_iid, body=note
        )
        return

    source_type, source_id, event_type = notification_source_for_watch(session, sha)

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
