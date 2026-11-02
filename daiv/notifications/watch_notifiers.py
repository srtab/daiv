from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.urls import reverse
from django.utils.translation import gettext as _

from asgiref.sync import async_to_sync
from sessions.models import EventKind, Run, RunStatus

from notifications.channels.registry import enabled_channel_types
from notifications.policy import notification_source_for_watch
from notifications.run_notifiers import deliver_to_recipients

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sessions.models import MergeRequestOwnership, Session

logger = logging.getLogger("daiv.notifications")


def _explanation(ownership: MergeRequestOwnership) -> str:
    """What the last finished run a CI failure woke on the merge request said it found, with repository paths relative
    to the repository; ``""`` when none did."""
    from automation.agent.utils import repo_relative_text

    run = (
        Run.objects
        .filter(
            repo_id=ownership.repo_id,
            status__in=RunStatus.terminal(),
            inbox_events__kind=EventKind.CI_FAILED,
            inbox_events__wake=True,
            inbox_events__merge_request_iid=ownership.merge_request_iid,
        )
        .select_related("task_result")
        .order_by("-created_at")
        .first()
    )
    if run is None:
        return ""
    return repo_relative_text((run.result_summary or run.response_text).strip())


def _render_payload(
    ownership: MergeRequestOwnership, failing_jobs: Sequence[str], pipeline_url: str, explanation: str
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
    if explanation:
        context["summary"] = explanation
    return subject, body, context


def emit_watch_exhausted(
    *, ownership: MergeRequestOwnership, session: Session, failing_jobs: Sequence[str], pipeline_url: str, sha: str
) -> None:
    """Tell the merge request's owner that the CI watch gave up on DAIV's latest commit, linking the owning session,
    with what the last run a CI failure woke on it found (SC-007), when one did.

    The caller only reports that the watch gave up; the source key, channels, dedup and payload are decided here so
    this event behaves like every other one. An ownerless merge request, which webhook-origin sessions often leave,
    gets the outcome as a merge request comment instead, without the explanation: the run's final message is on the
    merge request already (:mod:`sessions.inbox.complete`).
    """
    from sessions.pipeline_watch.platform import WatchPlatform

    recipient = ownership.owner
    explanation = _explanation(ownership) if recipient is not None else ""
    subject, body, context = _render_payload(ownership, failing_jobs, pipeline_url, explanation)
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
        body=f"{body}\n\n{explanation}" if explanation else body,
        link_url=reverse("session_detail", kwargs={"thread_id": session.thread_id}),
        channels=enabled_channel_types(),
        context=context,
    )
