from datetime import datetime, timedelta
from functools import reduce
from operator import or_
from typing import TYPE_CHECKING, NamedTuple

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.db.models import Case, Count, Exists, IntegerField, OuterRef, Q, Value, When
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from notifications.choices import EnvelopeStatus
from notifications.outcomes import CANT_RUN, CI_STILL_FAILING, ENVELOPE_OUTCOMES, Outcome
from sessions.filters import SCOPE_MINE
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState

from codebase.base import GitPlatform
from codebase.conf import settings as codebase_settings
from core.conf import settings as core_settings
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL
from dashboard import insights
from dashboard.context_processors import running_jobs_count
from dashboard.tasks import BOT_USERNAME_CACHE_KEY, BOT_USERNAME_RETRY_TIMEOUT, resolve_bot_username_task
from schedules.models import Frequency, ScheduledJob

if TYPE_CHECKING:
    from accounts.models import User


def homepage(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "dashboard/homepage.html")


INBOX_LIMIT = 8
RECENT_SESSIONS_LIMIT = 5
# Findings and a given-up CI watch have no resolving event DAIV can see (a person may fix them by
# hand), so they age out; a failure or a question stays until a newer run supersedes it, or INBOX_MAX_AGE.
INBOX_FINDINGS_WINDOW = timedelta(days=7)
INBOX_MAX_AGE = timedelta(days=90)

BOT_USERNAME_PENDING_KEY = "dashboard:bot-username-pending"

_SEVERITY: dict[str, int] = {status: rank for rank, status in enumerate(EnvelopeStatus.worst_first())}
_SEVERITY |= {CI_STILL_FAILING.key: _SEVERITY[EnvelopeStatus.FAILED], CANT_RUN.key: _SEVERITY[EnvelopeStatus.FAILED]}
_OUTCOMES_BY_KEY = {outcome.key: outcome for outcome in (*ENVELOPE_OUTCOMES.values(), CI_STILL_FAILING, CANT_RUN)}

SLASH_COMMANDS = [
    ("/help", _("List the commands and skills available here")),
    ("/plan", _("Explore the code and post an implementation plan")),
    ("/code-review", _("Review the changes")),
    ("/clear", _("Start the conversation over")),
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


def _inbox_rules(since: datetime) -> list[tuple[Q, Outcome]]:
    """Each way a run of yours can leave you something to do, worst first, so the first match is the one shown."""
    return [
        (
            Q(status=RunStatus.FAILED, cancel_requested_at__isnull=True, retried=False),
            ENVELOPE_OUTCOMES[EnvelopeStatus.FAILED],
        ),
        (Q(session__watch_state=WatchState.EXHAUSTED, session__last_active_at__gte=since), CI_STILL_FAILING),
        (Q(status=RunStatus.WAITING_INPUT), ENVELOPE_OUTCOMES[EnvelopeStatus.NEEDS_INPUT]),
        *(
            (Q(envelope__status=status, finished_at__gte=since), ENVELOPE_OUTCOMES[status])
            for status in (EnvelopeStatus.FOUND_ISSUES, EnvelopeStatus.NEEDS_ATTENTION)
        ),
    ]


def _runs_needing(user: User, since: datetime) -> list[dict]:
    """The user's runs that are the newest in their session and left them something to do, worst first.

    A newer run in the session (an answer, a follow-up that succeeded) clears it, whoever started it.
    A failed job's Retry starts a new session, so a later run of the same schedule, or a later run of
    the user's on the same repository with the same prompt, counts as having retried it.
    """
    newer = Run.objects.filter(session=OuterRef("session")).filter(
        Q(created_at__gt=OuterRef("created_at")) | Q(created_at=OuterRef("created_at"), id__gt=OuterRef("id"))
    )
    later = Q(created_at__gt=OuterRef("created_at")) & ~Q(session=OuterRef("session"))
    rerun = Run.objects.filter(later, session__scheduled_job=OuterRef("session__scheduled_job"))
    retry = (
        Run.objects
        .started_by(user)
        .filter(later, trigger_type__in=SessionOrigin.prompt_driven())
        .filter(repo_id=OuterRef("repo_id"), prompt=OuterRef("prompt"))
        .exclude(prompt="")
    )
    rules = _inbox_rules(since)
    return list(
        Run.objects
        .started_by(user)
        .filter(session__last_active_at__gte=timezone.now() - INBOX_MAX_AGE)
        .exclude(Exists(newer))
        .annotate(retried=Exists(rerun) | Exists(retry))
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


def _failing_schedules(user: User):
    """The user's own schedules that can't start a run (``ScheduledJob.is_failing``)."""
    return (
        ScheduledJob.objects
        .filter(user=user, failing_since__isnull=False)
        .filter(Q(is_enabled=True) | Q(frequency=Frequency.ONCE))
        .order_by("-failing_since")
    )


def _bot_username() -> str:
    """The account DAIV answers to on the git platform; the label it shares a name with until a task resolves it."""
    username = cache.get(BOT_USERNAME_CACHE_KEY)
    if username is None and cache.add(BOT_USERNAME_PENDING_KEY, True, BOT_USERNAME_RETRY_TIMEOUT):
        resolve_bot_username_task.enqueue()
        username = cache.get(BOT_USERNAME_CACHE_KEY)
    return username or BOT_LABEL


MR_LIST_LIMIT = 3

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


def _my_sessions_url(**params: str) -> str:
    return insights.session_list_url(params, scope=SCOPE_MINE)


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


def your_week(user: User) -> dict:
    """What the runs ``user`` started came to over the last 7 days, a column a day.

    Counts runs, so one session can add to several days; each day opens the sessions that ran that day.
    """
    window = insights.window_for("7d")
    runs = Run.objects.started_by(user)
    counted = runs.filter(window.date_q("created_at")).filter(reduce(or_, (q for *_rest, q in WEEK_OUTCOMES)))
    chart = insights.stacked_chart(
        runs, "created_at", window, WEEK_OUTCOMES, lambda day: _my_sessions_url(ran_on=day.end.isoformat())
    )
    sessions = counted.aggregate(n=Count("session", distinct=True))["n"]

    merge_requests = {}
    for repo_id, iid, url, title in (
        runs
        .filter(window.date_q("created_at"), merge_request_iid__isnull=False)
        .order_by("-created_at")
        .values_list("repo_id", "merge_request_iid", "merge_request_web_url", "session__title")
    ):
        merge_request = merge_requests.setdefault((repo_id, iid), {"repo_id": repo_id, "iid": iid, "title": title})
        merge_request["url"] = merge_request.get("url") or url

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
        "merge_requests": list(merge_requests.values())[:MR_LIST_LIMIT],
        "merge_requests_total": len(merge_requests),
        "merge_requests_more": max(len(merge_requests) - MR_LIST_LIMIT, 0),
        "merge_requests_more_url": _my_sessions_url(**window.list_params()),
        "started_from": started_from,
    }


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["inbox"] = self._get_inbox(user)
        context["recent_sessions"] = self._get_recent_sessions(user)
        context["recent_in_flight_ids"] = ",".join(
            str(run.id)
            for session in context["recent_sessions"]
            for run in session.runs.all()
            if run.status not in RunStatus.terminal()
        )
        context["my_sessions_url"] = _my_sessions_url()
        context["your_week"] = your_week(user) if context["recent_sessions"] else None
        context["ways"] = self._get_ways_to_use()
        return context

    def _get_inbox(self, user: User) -> dict:
        runs = _runs_needing(user, timezone.now() - INBOX_FINDINGS_WINDOW)
        schedules = list(_failing_schedules(user))
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
            {"label": label, "url": _my_sessions_url(status=status)}
            for label, status, key in (
                (_("All your failed sessions"), RunStatus.FAILED, EnvelopeStatus.FAILED),
                (_("All needing your input"), RunStatus.WAITING_INPUT, EnvelopeStatus.NEEDS_INPUT),
            )
            if key in outcomes
        ]
        if schedules:
            see_all_links.append({"label": _("Your schedules"), "url": reverse("schedule_list")})
        return {"items": items[:INBOX_LIMIT], "total": len(runs) + len(schedules), "see_all": see_all_links}

    def _get_recent_sessions(self, user: User) -> list[Session]:
        return list(
            Session.objects
            .listed_for(user, mine=True)
            .with_row_runs()
            .order_by("-last_active_at")[:RECENT_SESSIONS_LIMIT]
        )

    def _get_ways_to_use(self) -> dict:
        platform_name, mr_term = _platform_terms()
        mcp_url = f"{str(core_settings.EXTERNAL_URL).rstrip('/')}/mcp/"
        return {
            "platform_name": platform_name,
            "mr_term": mr_term,
            "platform_icon": str(codebase_settings.CLIENT),
            "bot_mention": f"@{_bot_username()}",
            "labels": [
                {"name": BOT_LABEL, "help": _("Plans, then waits for your go-ahead")},
                {"name": BOT_AUTO_LABEL, "help": _("Plans and implements without waiting")},
                {"name": BOT_MAX_LABEL, "help": _("Uses a more capable model; combine with either")},
            ],
            "slash_commands": [{"name": name, "help": help_text} for name, help_text in SLASH_COMMANDS],
            "mcp_url": mcp_url,
            "mcp_command": f"claude mcp add daiv --transport http {mcp_url}",
        }


def _period(request) -> str:
    period = request.GET.get("period", insights.DEFAULT_PERIOD)
    return period if period in insights.PERIOD_DAYS else insights.DEFAULT_PERIOD


class InsightsView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/insights.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        period = _period(self.request)
        window = insights.window_for(period)
        previous = window.previous()
        context.update({
            "periods": [{"key": key, "label": label} for key, label, _days in insights.PERIOD_CHOICES],
            "current_period": period,
            "window": window,
            "previous_window": previous,
            "activity": insights.activity(user, window, previous),
            "running": running_jobs_count(self.request, user),
            "breakdown": insights.session_breakdown(user, window, previous),
            "velocity": insights.velocity(user, window),
        })
        return context
