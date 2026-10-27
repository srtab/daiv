"""What the session page's top bar and details panel show beyond the session's own columns: the latest run's status
and retry link, the CI watch on its merge request, and the issue an issue-webhook session addresses.

``latest_run_payload`` and ``watch_payload`` are also what the turns poll returns, so what the page rendered and what
keeps it current are built by the same code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from codebase.base import GitPlatform
from codebase.conf import settings as codebase_settings
from codebase.models import RepositoryCatalog
from sessions.models import SessionOrigin, WatchState
from sessions.templatetags.session_tags import status_variant
from sessions.transcript import retry_urls

if TYPE_CHECKING:
    from accounts.models import User
    from sessions.models import Run, Session

_WATCH_TONES = {
    WatchState.WATCHING: "attn",
    WatchState.FIXING: "attn",
    WatchState.GREEN: "clear",
    WatchState.EXHAUSTED: "fail",
    WatchState.UNCLEAR: "found",
}


def latest_run_payload(runs: list[Run], user: User) -> dict[str, str] | None:
    """SYNC ONLY: the newest of ``runs`` (oldest first) as the header's status pill and Retry link reads it."""
    if not runs:
        return None
    latest = runs[-1]
    return {
        "id": str(latest.id),
        "status": latest.status,
        "label": str(latest.get_status_display()),
        "variant": status_variant(latest.status),
        "finished_at": latest.finished_at.isoformat() if latest.finished_at else "",
        "retry_url": retry_urls([latest], user).get(str(latest.id), ""),
    }


def watch_payload(session: Session) -> dict[str, str] | None:
    """The CI watch on the session's merge request, or ``None`` when nothing is watched."""
    tone = _WATCH_TONES.get(session.watch_state)
    if tone is None:
        return None
    return {"state": session.watch_state, "label": str(session.get_watch_state_display()), "tone": tone}


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


def build_session_header(session: Session, runs: list[Run], user: User) -> dict[str, Any]:
    """SYNC ONLY: the header's context, for ``runs`` ordered oldest first."""
    return {
        "latest_run": latest_run_payload(runs, user),
        "watch": watch_payload(session),
        "issue": issue_link(session, runs),
        "run_count": len(runs),
    }
