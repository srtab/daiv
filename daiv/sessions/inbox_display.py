"""How an inbox event reads on the session page: the bubble that shows a request a run takes or took, and why an event
DAIV was only told about did not start one. Built from the event's payload alone, in the viewer's language."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from sessions.inbox.policy import MAX_SYSTEM_RUNS_IN_A_ROW
from sessions.inbox.render import EventText
from sessions.models import EventKind, QuietReason

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sessions.models import InboxEvent, Run

RECEIVED_KINDS = frozenset({EventKind.CI_FAILED, EventKind.DELEGATION_FINISHED})


def reason_text(kind: str, reason: str, payload: Mapping[str, Any] | None = None) -> str:
    """Why an event of ``kind`` did not wake the agent, for its timeline line; ``""`` with no ``reason``. A give-up
    says how many attempts were spent and which jobs fail when its ``payload`` records them."""
    if not reason:
        return ""
    text = _gave_up(payload or {}) if reason == QuietReason.BUDGET_SPENT else _reason(reason)
    if kind == EventKind.CI_FAILED:
        return _("not fixing: %(reason)s") % {"reason": text}
    if kind == EventKind.DELEGATION_FINISHED:
        return _("not acting: %(reason)s") % {"reason": text}
    return text


def _reason(reason: str) -> str:
    match reason:
        case QuietReason.NOT_DAIV_COMMIT:
            return _("not DAIV's commit")
        case QuietReason.ALREADY_EXAMINED:
            return _("DAIV already looked at this commit")
        case QuietReason.BUDGET_SPENT:
            return _("out of attempts")
        case QuietReason.WATCH_OFF:
            return _("CI fixing is off")
        case QuietReason.RUN_LIMIT:
            return _("paused after %(n)s runs in a row") % {"n": MAX_SYSTEM_RUNS_IN_A_ROW}
    return reason


def stopped_after(attempts: int) -> str:
    """That CI fixing gave up after ``attempts`` attempts, in lower case: ``capfirst`` it to start a sentence."""
    return ngettext("stopped after %(n)s attempt", "stopped after %(n)s attempts", attempts) % {"n": attempts}


def _gave_up(payload: Mapping[str, Any]) -> str:
    if (attempts := payload.get("attempts")) is None:
        return _reason(QuietReason.BUDGET_SPENT)
    text = stopped_after(attempts)
    if jobs := payload.get("failed_jobs"):
        return _("%(stopped)s, failing: %(jobs)s") % {"stopped": text, "jobs": ", ".join(jobs)}
    return text


def received_summary(event: InboxEvent) -> str:
    """The one line a CI failure or a finished delegated batch shows in its received bubble; ``""`` for a batch that
    recorded no outcome, whose bubble's source line says it all."""
    if event.kind == EventKind.DELEGATION_FINISHED:
        if (total := event.payload.get("total")) is None:
            return ""
        return ngettext(
            "%(succeeded)s of %(total)s job succeeded", "%(succeeded)s of %(total)s jobs succeeded", total
        ) % {"succeeded": event.payload["succeeded"], "total": total}
    pipeline_id, jobs = event.payload["pipeline_id"], event.payload.get("failed_jobs") or []
    names = {"id": pipeline_id, "workflow": event.payload.get("workflow"), "jobs": ", ".join(jobs)}
    if names["workflow"]:
        return (_("%(workflow)s run %(id)s failed: %(jobs)s") if jobs else _("%(workflow)s run %(id)s failed")) % names
    if jobs:
        return _("Pipeline %(id)s failed: %(jobs)s") % names
    return _("Pipeline %(id)s failed") % names


def bubble(event: InboxEvent) -> dict[str, Any]:
    """``event`` as a bubble that starts a run: a person's full words, or a received summary."""
    payload = event.payload
    received = event.kind in RECEIVED_KINDS
    match event.kind:
        case EventKind.CHAT_MESSAGE:
            text = payload["text"]
        case EventKind.FOLLOW_UP_JOB:
            text = payload["prompt"]
        case EventKind.MENTION:
            text = f"{EventText(event).author_name()} mentioned you on !{event.merge_request_iid}: {payload['body']}"
        case _:
            text = received_summary(event) if received else ""
    return {
        "id": f"inbox-{event.pk}",
        "event_id": str(event.pk),
        "kind": event.kind,
        "received": received,
        "failed": event.kind == EventKind.CI_FAILED or payload.get("succeeded", 0) < payload.get("total", 0),
        "text": text,
        "source_iid": event.merge_request_iid,
        "trigger_type": payload.get("trigger_type", "") if event.kind == EventKind.FOLLOW_UP_JOB else "",
        "web_url": payload.get("web_url", ""),
        "created_at": event.created_at.isoformat(),
    }


def run_bubble(run: Run) -> dict[str, Any]:
    """A queued job run with no inbox event, as the bubble of its prompt."""
    return {
        "id": f"run-{run.id}",
        "event_id": "",
        "kind": "job",
        "received": False,
        "failed": False,
        "text": run.prompt,
        "source_iid": None,
        "trigger_type": run.trigger_type,
        "web_url": "",
        "created_at": run.created_at.isoformat(),
    }
