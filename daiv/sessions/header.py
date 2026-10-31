"""What the session page's top bar and details panel show beyond the session's own columns: the latest run's status
and what its Retry sends, CI on the merge requests the session owns, and the issue an issue-webhook session
addresses.

``latest_run_payload`` and ``ci_payload`` are also what the turns poll returns, so what the page rendered and what
keeps it current are built by the same code.

CI is reported per merge request the session owns (``MergeRequestOwnership``), from the latest CI event in its inbox:
``failing``, ``passing``, ``stopped`` (a status that needs a person), or ``running`` while no result has arrived since
DAIV last published or a run is looking at a failure. A failure links the last run that looked at one
(``explanation_run_id``), whose summary says what it found. ``watch_payload`` keeps the single-watch shape older pages
read, from the first merge request, for one release.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.utils.translation import gettext as _

from asgiref.sync import sync_to_async

from codebase.authorization import can_run
from codebase.base import GitPlatform
from codebase.conf import settings as codebase_settings
from codebase.models import RepositoryCatalog
from codebase.repo_config import RepositoryConfig
from sessions.models import EventKind, InboxEvent, MergeRequestOwnership, RunStatus, Session, SessionOrigin
from sessions.pipeline_watch.policy import WatchPolicy
from sessions.pipeline_watch.store import CI_KINDS
from sessions.templatetags.session_tags import status_variant
from sessions.transcript import marker_id, retry_message

if TYPE_CHECKING:
    from accounts.models import User
    from sessions.models import Run

_CI_STATUS_BY_KIND = {EventKind.CI_FAILED: "failing", EventKind.CI_PASSED: "passing", EventKind.CI_STOPPED: "stopped"}
_CI_TONES = {"failing": "fail", "passing": "clear", "running": "attn", "stopped": "found"}


def _ci_label(status: str) -> str:
    return {
        "failing": _("Failing"),
        "passing": _("Passing"),
        "running": _("Waiting for CI"),
        "stopped": _("Needs a person"),
    }[status]


def retry_for(
    session: Session, runs: list[Run], turns: list[dict[str, Any]], user: User, *, can_act: bool | None = None
) -> str:
    """SYNC ONLY: ``retry_message`` for a ``user`` the chat endpoint lets post to ``session``, else empty.

    ``can_act`` is ``can_run`` on the session's repository, for a caller that already has it.
    """
    if not (message := retry_message(turns, runs)):
        return ""
    if can_act is None:
        can_act = can_run(user, session.repo_id)
    if not can_act or not Session.objects.by_owner(user).filter(pk=session.pk).exists():
        return ""
    return message


def latest_run_payload(runs: list[Run], retry: str) -> dict[str, str] | None:
    """The newest of ``runs`` (oldest first) as the header's status pill and the Retry on its marker read it.

    ``retry`` is what ``retry_for`` returned for the same runs.
    """
    if not runs:
        return None
    latest = runs[-1]
    return {
        "marker_id": marker_id(latest),
        "label": str(latest.get_status_display()),
        "variant": status_variant(latest.status),
        "finished_at": latest.finished_at.isoformat() if latest.finished_at else "",
        "retry": retry,
    }


def ci_payload(session: Session) -> list[dict[str, Any]]:
    """SYNC ONLY: CI on each open merge request ``session`` owns, the most recently claimed first."""
    ownerships = list(
        MergeRequestOwnership.objects.filter(session=session, closed_at__isnull=True).order_by("-updated_at", "-pk")
    )
    if not ownerships:
        return []
    policy = WatchPolicy(RepositoryConfig.get_config(session.repo_id))
    return [_ci_item(session, ownership, policy) for ownership in ownerships]


aci_payload = sync_to_async(ci_payload)


def _ci_item(session: Session, ownership: MergeRequestOwnership, policy: WatchPolicy) -> dict[str, Any]:
    events = InboxEvent.objects.filter(session=session, merge_request_iid=ownership.merge_request_iid)
    latest = events.filter(kind__in=CI_KINDS).order_by("-created_at").first()
    woke = events.filter(kind=EventKind.CI_FAILED, wake=True, run__isnull=False).order_by("-created_at")
    looking = woke.exclude(run__status__in=RunStatus.terminal()).exists()
    if latest is None or latest.created_at < ownership.updated_at or looking:
        status = "running"
    else:
        status = _CI_STATUS_BY_KIND[latest.kind]
    explanation = woke.values_list("run_id", flat=True).first()
    return {
        "merge_request_iid": ownership.merge_request_iid,
        "status": status,
        "label": str(_ci_label(status)),
        "tone": _CI_TONES[status],
        "attempts": ownership.attempts,
        "max_attempts": policy.max_attempts,
        "watch_enabled": ownership.watch_enabled and policy.enabled,
        "explanation_run_id": str(explanation) if explanation else None,
    }


def watch_payload(ci: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first merge request's CI in the single-watch shape older pages read; ``open`` while its watch is on."""
    if not ci:
        return None
    first = ci[0]
    return {"state": first["status"], "label": first["label"], "tone": first["tone"], "open": first["watch_enabled"]}


def issue_link(session: Session, runs: list[Run]) -> dict[str, Any] | None:
    """SYNC ONLY: the issue an issue-webhook session addresses, read off the task that started its first issue run.

    The thread id is a hash of the issue, so the task's ``issue_iid`` kwarg is the only record of which one it was.
    ``url`` is blank when the repository is not in the catalog, which is where the platform's web URL comes from.
    """
    if session.origin != SessionOrigin.ISSUE_WEBHOOK:
        return None
    run = next((r for r in runs if r.trigger_type == SessionOrigin.ISSUE_WEBHOOK and r.task_result_id), None)
    if run is None:
        return None
    iid = ((run.task_result.args_kwargs or {}).get("kwargs") or {}).get("issue_iid")
    if not iid:
        return None
    html_url = (
        RepositoryCatalog.objects
        .filter(provider=codebase_settings.CLIENT.value, slug=session.repo_id)
        .values_list("html_url", flat=True)
        .first()
    )
    if not html_url:
        return {"iid": iid, "url": ""}
    path = "-/issues" if codebase_settings.CLIENT == GitPlatform.GITLAB else "issues"
    return {"iid": iid, "url": f"{html_url.rstrip('/')}/{path}/{iid}"}


def build_session_header(
    session: Session, runs: list[Run], *, retry: str, merge_request: dict | None = None
) -> dict[str, Any]:
    """SYNC ONLY: the header's context, for ``runs`` ordered oldest first and the ``merge_request`` the page shows."""
    ci = ci_payload(session)
    return {
        "latest_run": latest_run_payload(runs, retry),
        "ci": ci,
        "watch": watch_payload(ci),
        "issue": issue_link(session, runs),
        "run_count": len(runs),
    }
