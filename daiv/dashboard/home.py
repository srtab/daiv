"""What the Home page shows the signed-in user: Needs you, recent sessions, Your week and Ways to use DAIV."""

from datetime import datetime, timedelta
from functools import reduce
from operator import or_
from typing import TYPE_CHECKING, NamedTuple

from django.db.models import Case, Count, IntegerField, Q, QuerySet, Value, When
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from notifications.choices import EnvelopeStatus
from notifications.outcomes import CANT_RUN, CI_STILL_FAILING, ENVELOPE_OUTCOMES, Outcome
from sessions.filters import SCOPE_MINE
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState

from codebase.base import GitPlatform
from codebase.conf import settings as codebase_settings
from core.conf import settings as core_settings
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL
from dashboard import insights
from dashboard.tasks import bot_username
from schedules.models import ScheduledJob

if TYPE_CHECKING:
    from accounts.models import User


INBOX_LIMIT = 8
RECENT_SESSIONS_LIMIT = 5
# Each item stays until a newer run supersedes it, but findings and a given-up CI watch leave after 7 days and a
# failure 14 days after it finished; a question leaves only at INBOX_MAX_AGE, a schedule when it can run again.
INBOX_FINDINGS_WINDOW = timedelta(days=7)
INBOX_FAILED_WINDOW = timedelta(days=14)
INBOX_MAX_AGE = timedelta(days=90)
MR_LIST_LIMIT = 3

# Not EnvelopeStatus.worst_first(): a question blocks a run and takes seconds to answer, so it leads the inbox.
_INBOX_RANKS: tuple[tuple[str, ...], ...] = (
    (EnvelopeStatus.NEEDS_INPUT,),
    (EnvelopeStatus.FAILED, CI_STILL_FAILING.key, CANT_RUN.key),
    (EnvelopeStatus.FOUND_ISSUES,),
    (EnvelopeStatus.NEEDS_ATTENTION,),
)
_SEVERITY: dict[str, int] = {key: rank for rank, keys in enumerate(_INBOX_RANKS) for key in keys}
_OUTCOMES_BY_KEY = {outcome.key: outcome for outcome in (*ENVELOPE_OUTCOMES.values(), CI_STILL_FAILING, CANT_RUN)}

SLASH_COMMANDS = [
    ("/help", _("List the commands and skills available here")),
    ("/plan", _("Explore the code and post an implementation plan")),
    ("/code-review", _("Review the changes")),
    ("/clear", _("Start the conversation over")),
]

_FLAGGED = Q(envelope__status__in=[EnvelopeStatus.FOUND_ISSUES, EnvelopeStatus.NEEDS_ATTENTION])
# Worst last: the chart stacks the last kind on top.
WEEK_OUTCOMES = [
    ("changed", _("Changed code"), "bg-status-clear", Q(status=RunStatus.SUCCESSFUL, code_changes=True) & ~_FLAGGED),
    ("done", _("Done, no changes"), "bg-text-muted", Q(status=RunStatus.SUCCESSFUL, code_changes=False) & ~_FLAGGED),
    (
        "needed",
        _("Needed you"),
        "bg-status-found",
        Q(status=RunStatus.WAITING_INPUT) | (Q(status=RunStatus.SUCCESSFUL) & _FLAGGED),
    ),
    ("failed", _("Failed"), "bg-status-fail", Q(status=RunStatus.FAILED, cancel_requested_at__isnull=True)),
]


class InboxItem(NamedTuple):
    outcome: Outcome
    title: str
    detail: str
    url: str
    at: datetime | None

    @property
    def severity(self) -> int:
        return _SEVERITY[self.outcome.key]


def my_sessions_url(**params: str) -> str:
    return insights.session_list_url(params, scope=SCOPE_MINE)


def _inbox_rules(now: datetime) -> list[tuple[Q, Outcome]]:
    """Each way a run of yours can leave you something to do, in inbox order, so the first match is the one shown."""
    since = now - INBOX_FINDINGS_WINDOW
    failed_since = now - INBOX_FAILED_WINDOW
    rules = [
        (Q(status=RunStatus.WAITING_INPUT), ENVELOPE_OUTCOMES[EnvelopeStatus.NEEDS_INPUT]),
        (
            Q(status=RunStatus.FAILED, cancel_requested_at__isnull=True, retried=False)
            & (Q(finished_at__gte=failed_since) | Q(finished_at__isnull=True, created_at__gte=failed_since)),
            ENVELOPE_OUTCOMES[EnvelopeStatus.FAILED],
        ),
        (Q(session__watch_state=WatchState.EXHAUSTED, session__last_active_at__gte=since), CI_STILL_FAILING),
        *(
            (Q(envelope__status=status, finished_at__gte=since), ENVELOPE_OUTCOMES[status])
            for status in (EnvelopeStatus.FOUND_ISSUES, EnvelopeStatus.NEEDS_ATTENTION)
        ),
    ]
    return sorted(rules, key=lambda rule: _SEVERITY[rule[1].key])


def _runs_needing(user: User) -> list[dict]:
    """The user's runs that are the newest in their session and left them something to do, in inbox order."""
    now = timezone.now()
    rules = _inbox_rules(now)
    return list(
        Run.objects
        .started_by(user)
        .filter(session__last_active_at__gte=now - INBOX_MAX_AGE)
        .latest_in_session()
        .with_retried(user)
        .annotate(
            inbox_outcome=Case(*(When(q, then=Value(outcome.key)) for q, outcome in rules), default=None),
            inbox_rank=Case(
                *(When(q, then=Value(_SEVERITY[outcome.key])) for q, outcome in rules), output_field=IntegerField()
            ),
        )
        .filter(inbox_outcome__isnull=False)
        .order_by("inbox_rank", "-session__last_active_at")
        .values("inbox_outcome", "session_id", "session__title", "session__repo_id", "session__last_active_at")
    )


def inbox(user: User) -> dict:
    """Needs you: what is waiting on ``user``, questions first, with links to the full lists behind it."""
    runs = _runs_needing(user)
    schedules = list(ScheduledJob.objects.filter(user=user).failing().order_by("-failing_since"))
    items = [
        InboxItem(
            outcome=_OUTCOMES_BY_KEY[run["inbox_outcome"]],
            title=run["session__title"] or run["session__repo_id"],
            detail=run["session__repo_id"] if run["session__title"] else "",
            url=reverse("session_detail", kwargs={"thread_id": run["session_id"]}),
            at=run["session__last_active_at"],
        )
        for run in runs[:INBOX_LIMIT]
    ] + [
        InboxItem(
            outcome=CANT_RUN,
            title=schedule.name,
            detail=schedule.dispatch_error_message,
            url=reverse("schedule_update", kwargs={"pk": schedule.pk}),
            at=schedule.failing_since,
        )
        for schedule in schedules[:INBOX_LIMIT]
    ]
    items.sort(key=lambda item: (item.severity, -(item.at.timestamp() if item.at else 0)))

    outcomes = {run["inbox_outcome"] for run in runs}
    see_all_links = [
        {"label": label, "url": my_sessions_url(status=status)}
        for label, status, key in (
            (_("All your failed sessions"), RunStatus.FAILED, EnvelopeStatus.FAILED),
            (_("All needing your input"), RunStatus.WAITING_INPUT, EnvelopeStatus.NEEDS_INPUT),
        )
        if key in outcomes
    ]
    if schedules:
        see_all_links.append({"label": _("Your schedules"), "url": reverse("schedule_list")})
    return {"items": items[:INBOX_LIMIT], "total": len(runs) + len(schedules), "see_all": see_all_links}


def recent_sessions(user: User) -> list[Session]:
    return list(
        Session.objects.listed_for(user, mine=True).with_row_runs().order_by("-last_active_at")[:RECENT_SESSIONS_LIMIT]
    )


def in_flight_run_ids(sessions: list[Session]) -> str:
    """The unfinished runs of ``sessions``, comma-separated, for the live status stream."""
    return ",".join(
        str(run.id) for session in sessions for run in session.runs.all() if run.status not in RunStatus.terminal()
    )


def _platform_terms() -> tuple[str, str]:
    if codebase_settings.CLIENT == GitPlatform.GITHUB:
        return "GitHub", "pull request"
    return "GitLab", "merge request"


def _started_from() -> list[tuple[str, frozenset[str]]]:
    """Every trigger in exactly one group, so the counts add up to the week's runs."""
    groups = [
        (_platform_terms()[0], SessionOrigin.webhooks() | {SessionOrigin.PIPELINE_WEBHOOK}),
        (str(_("Your editor")), frozenset({SessionOrigin.MCP_JOB})),
        (str(_("Here")), frozenset({SessionOrigin.CHAT, SessionOrigin.UI_JOB})),
        (str(_("Schedules")), frozenset({SessionOrigin.SCHEDULE})),
        (str(_("API")), frozenset({SessionOrigin.API_JOB})),
    ]
    grouped = frozenset().union(*(origins for _label, origins in groups))
    return [*groups, (str(_("Delegated by the agent")), frozenset(SessionOrigin.values) - grouped)]


def _merge_requests(runs: QuerySet[Run]) -> list[dict]:
    """One entry per merge request ``runs`` worked on, newest first, titled by its newest run's session."""
    merge_requests: dict[tuple[str, int], dict] = {}
    for repo_id, iid, url, title in (
        runs
        .filter(merge_request_iid__isnull=False)
        .order_by("-created_at")
        .values_list("repo_id", "merge_request_iid", "merge_request_web_url", "session__title")
    ):
        merge_request = merge_requests.setdefault((repo_id, iid), {"repo_id": repo_id, "iid": iid, "title": title})
        merge_request["url"] = merge_request.get("url") or url
    return list(merge_requests.values())


def your_week(user: User) -> dict:
    """What the runs ``user`` started came to over the last 7 days, a column a day.

    Counts runs, so one session can add to several days; each day opens the sessions that ran that day.
    """
    window = insights.window_for("7d")
    runs = Run.objects.started_by(user)
    in_window = runs.filter(window.date_q("created_at"))
    counted = in_window.filter(reduce(or_, (q for *_rest, q in WEEK_OUTCOMES)))
    chart = insights.stacked_chart(
        runs, "created_at", window, WEEK_OUTCOMES, lambda day: my_sessions_url(ran_on=day.end.isoformat())
    )
    sessions = counted.aggregate(n=Count("session", distinct=True))["n"]
    merge_requests = _merge_requests(in_window)
    triggers = counted.order_by().values("trigger_type").annotate(n=Count("pk"))
    by_trigger = dict(triggers.values_list("trigger_type", "n"))
    started_from = [
        {"label": label, "count": n}
        for label, origins in _started_from()
        if (n := sum(by_trigger.get(origin, 0) for origin in origins))
    ]
    return {
        "window": window,
        "chart": chart,
        "runs": sum(by_trigger.values()),
        "sessions": sessions,
        "merge_requests": merge_requests[:MR_LIST_LIMIT],
        "merge_requests_total": len(merge_requests),
        "merge_requests_more": max(len(merge_requests) - MR_LIST_LIMIT, 0),
        "merge_requests_more_url": my_sessions_url(**window.list_params()),
        "started_from": started_from,
    }


def ways_to_use() -> dict:
    """The three ways into DAIV, specific to this instance's git platform, bot account and URL."""
    platform_name, mr_term = _platform_terms()
    mcp_url = f"{str(core_settings.EXTERNAL_URL).rstrip('/')}/mcp/"
    return {
        "platform_name": platform_name,
        "mr_term": mr_term,
        "platform_icon": str(codebase_settings.CLIENT),
        "bot_mention": f"@{bot_username()}",
        "labels": [
            {"name": BOT_LABEL, "help": _("Plans, then waits for your go-ahead")},
            {"name": BOT_AUTO_LABEL, "help": _("Plans and implements without waiting")},
            {"name": BOT_MAX_LABEL, "help": _("Uses a more capable model; combine with either")},
        ],
        "slash_commands": [{"name": name, "help": help_text} for name, help_text in SLASH_COMMANDS],
        "mcp_url": mcp_url,
        "mcp_command": f"claude mcp add daiv --transport http {mcp_url}",
    }
