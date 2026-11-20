from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.urls import reverse
from django.utils.translation import gettext as _

from notifications.channels.registry import enabled_channel_types
from notifications.policy import notification_source_for_inbox_pause
from notifications.run_notifiers import deliver_to_recipients

if TYPE_CHECKING:
    from sessions.models import Session

logger = logging.getLogger("daiv.notifications")


def emit_inbox_paused(*, session: Session, first_blocked_event_id: str, runs_in_a_row: int) -> None:
    """Tell the session's owner that DAIV stopped waking the session on its own: it started ``runs_in_a_row`` runs in
    a row with no person asking, so CI failures and finished delegated batches are only remembered until someone
    sends a message or mentions it. Once per streak; a session with no owner tells no one."""
    recipient = session.user
    if recipient is None:
        logger.info("inbox: no one to tell that thread_id=%s paused after runs in a row", session.thread_id)
        return

    name = session.title or session.repo_id
    subject = _("DAIV paused on {name}").format(name=name)
    body = _(
        "DAIV ran {runs} times in a row on this session without anyone asking, so it stopped acting on CI failures "
        "and delegated jobs there. They're still remembered. Send a message or mention DAIV to pick it up again."
    ).format(runs=runs_in_a_row)
    source_type, source_id, event_type = notification_source_for_inbox_pause(session, first_blocked_event_id)
    deliver_to_recipients(
        [recipient],
        source_type=source_type,
        source_id=source_id,
        event_type=event_type,
        subject=subject,
        body=body,
        link_url=reverse("session_detail", kwargs={"thread_id": session.thread_id}),
        channels=enabled_channel_types(),
        context={
            "status_tone": "warning",
            "status_label": _("Paused"),
            "repo_id": session.repo_id,
            "session_title": name,
            "runs_in_a_row": runs_in_a_row,
        },
    )
