import logging
from datetime import datetime, timedelta
from functools import reduce
from operator import or_
from typing import TYPE_CHECKING, NamedTuple

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.db.models import Case, Count, Exists, IntegerField, OuterRef, Prefetch, Q, Subquery, Value, When
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from notifications.choices import EnvelopeStatus
from notifications.outcomes import CANT_RUN, CI_STILL_FAILING, ENVELOPE_OUTCOMES, Outcome
from sessions.filters import SCOPE_MINE
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState

from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.conf import settings as codebase_settings
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL
from dashboard import insights
from dashboard.context_processors import running_jobs_count
from schedules.models import Frequency, ScheduledJob

if TYPE_CHECKING:
    from accounts.models import User

logger = logging.getLogger("daiv.dashboard")


def homepage(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "dashboard/homepage.html")


INBOX_LIMIT = 8
RECENT_SESSIONS_LIMIT = 5
# Findings and a given-up CI watch have no resolving event DAIV can see (a person may fix them by
# hand), so they age out; a failure or a question stays until a newer run supersedes it.
INBOX_FINDINGS_WINDOW = timedelta(days=7)

BOT_USERNAME_CACHE_KEY = "dashboard:bot-username"
BOT_USERNAME_TIMEOUT = 60 * 60 * 24
BOT_USERNAME_RETRY_TIMEOUT = 60 * 5

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
    """Each way a session of yours can need you, worst first, so the first match is the one shown."""
    return [
        (
            Q(latest_run_status=RunStatus.FAILED, latest_run_cancel_requested_at__isnull=True, retried=False),
            ENVELOPE_OUTCOMES[EnvelopeStatus.FAILED],
        ),
        (Q(watch_state=WatchState.EXHAUSTED, last_active_at__gte=since), CI_STILL_FAILING),
        (Q(latest_run_status=RunStatus.WAITING_INPUT), ENVELOPE_OUTCOMES[EnvelopeStatus.NEEDS_INPUT]),
        *(
            (Q(latest_envelope_status=status, latest_run_finished_at__gte=since), ENVELOPE_OUTCOMES[status])
            for status in (EnvelopeStatus.FOUND_ISSUES, EnvelopeStatus.NEEDS_ATTENTION)
        ),
    ]


def _sessions_needing(user: User, since: datetime):
    """The user's sessions whose newest run left them something to do, annotated with ``inbox_outcome``.

    Everything is read off each session's newest run, so a newer run (an answer, a follow-up that
    succeeded) clears it. A failed job's Retry starts a new session, so a later run on the same
    repository with the same prompt counts as having retried it.
    """
    latest = Run.objects.filter(session=OuterRef("pk")).order_by("-created_at", "-id")
    retry = (
        Run.objects
        .filter(
            trigger_type__in=SessionOrigin.prompt_driven(),
            repo_id=OuterRef("latest_run_repo_id"),
            prompt=OuterRef("latest_run_prompt"),
            created_at__gt=OuterRef("latest_run_created_at"),
        )
        .exclude(session=OuterRef("pk"))
        .exclude(prompt="")
    )
    rules = _inbox_rules(since)
    return (
        Session.objects
        .listed_for(user, mine=True)
        .annotate(
            latest_run_cancel_requested_at=Subquery(latest.values("cancel_requested_at")[:1]),
            latest_run_finished_at=Subquery(latest.values("finished_at")[:1]),
            latest_run_created_at=Subquery(latest.values("created_at")[:1]),
            latest_run_repo_id=Subquery(latest.values("repo_id")[:1]),
            latest_run_prompt=Subquery(latest.values("prompt")[:1]),
            latest_envelope_status=Subquery(latest.values("envelope__status")[:1]),
            retried=Exists(retry),
        )
        .annotate(
            inbox_outcome=Case(*(When(q, then=Value(outcome.key)) for q, outcome in rules), default=None),
            inbox_rank=Case(
                *(When(q, then=Value(_SEVERITY[outcome.key])) for q, outcome in rules), output_field=IntegerField()
            ),
        )
        .filter(inbox_outcome__isnull=False)
        .order_by("inbox_rank", "-last_active_at")
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
    """The account DAIV answers to on the git platform; the label it shares a name with until that is known."""
    username = cache.get(BOT_USERNAME_CACHE_KEY)
    if username is not None:
        return username
    try:
        username = RepoClient.create_instance().current_user.username
    except Exception:
        logger.warning("Couldn't resolve DAIV's git platform username", exc_info=True)
        cache.set(BOT_USERNAME_CACHE_KEY, BOT_LABEL, BOT_USERNAME_RETRY_TIMEOUT)
        return BOT_LABEL
    cache.set(BOT_USERNAME_CACHE_KEY, username, BOT_USERNAME_TIMEOUT)
    return username


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
    return f"{reverse('session_list')}?{urlencode({'scope': SCOPE_MINE, **params})}"


def _started_from() -> list[tuple[str, frozenset[str]]]:
    """Every trigger in exactly one group, so the counts add up to the week's runs."""
    platform = "GitHub" if codebase_settings.CLIENT == GitPlatform.GITHUB else "GitLab"
    groups = [
        (platform, SessionOrigin.webhooks() | {SessionOrigin.PIPELINE_WEBHOOK}),
        (str(_("Your editor")), frozenset({SessionOrigin.MCP_JOB})),
        (str(_("Here")), frozenset({SessionOrigin.CHAT, SessionOrigin.UI_JOB})),
        (str(_("Schedules")), frozenset({SessionOrigin.SCHEDULE})),
        (str(_("API")), frozenset({SessionOrigin.API_JOB})),
    ]
    grouped = frozenset().union(*(origins for _label, origins in groups))
    return [*groups, (str(_("Delegated by the agent")), frozenset(SessionOrigin.values) - grouped)]


def your_week(user: User) -> dict:
    """What the runs in ``user``'s own sessions came to over the last 7 days, a column a day.

    Counts runs, so one session can add to several days; each day opens the sessions active that day.
    """
    window = insights.window_for("7d")
    runs = Run.objects.filter(session__in=Session.objects.listed_for(user, mine=True).values("pk"))
    counted = runs.filter(window.date_q("created_at")).filter(reduce(or_, (q for *_rest, q in WEEK_OUTCOMES)))
    chart = insights.stacked_chart(
        runs, "created_at", window, WEEK_OUTCOMES, lambda day: _my_sessions_url(**day.list_params(bounded=True))
    )
    totals = counted.aggregate(runs=Count("pk", distinct=True), sessions=Count("session", distinct=True))

    merge_requests = {}
    for repo_id, iid, url, title in (
        runs
        .filter(window.date_q("created_at"), merge_request_iid__isnull=False)
        .order_by("-created_at")
        .values_list("repo_id", "merge_request_iid", "merge_request_web_url", "session__title")
    ):
        merge_requests.setdefault((repo_id, iid), {"repo_id": repo_id, "iid": iid, "url": url, "title": title})

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
        "runs": totals["runs"],
        "sessions": totals["sessions"],
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
        since = timezone.now() - INBOX_FINDINGS_WINDOW
        sessions = _sessions_needing(user, since).only("thread_id", "title", "repo_id", "last_active_at")
        schedules = _failing_schedules(user)
        items = [
            InboxItem(
                outcome=_OUTCOMES_BY_KEY[session.inbox_outcome],
                title=session.title or session.repo_id,
                detail=session.repo_id if session.title else "",
                url=reverse("session_detail", kwargs={"thread_id": session.thread_id}),
                at=session.last_active_at,
            )
            for session in sessions[:INBOX_LIMIT]
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

        def see_all(status: str) -> str:
            return f"{reverse('session_list')}?{urlencode({'scope': SCOPE_MINE, 'status': status})}"

        outcome_counts = dict(
            sessions.order_by().values("inbox_outcome").annotate(n=Count("pk")).values_list("inbox_outcome", "n")
        )
        schedules_count = schedules.count()
        see_all_links = [
            {"label": label, "url": see_all(status)}
            for label, status, key in (
                (_("All your failed sessions"), RunStatus.FAILED, EnvelopeStatus.FAILED),
                (_("All needing your input"), RunStatus.WAITING_INPUT, EnvelopeStatus.NEEDS_INPUT),
            )
            if key in outcome_counts
        ]
        if schedules_count:
            see_all_links.append({"label": _("Your schedules"), "url": reverse("schedule_list")})
        return {
            "items": items[:INBOX_LIMIT],
            "total": sum(outcome_counts.values()) + schedules_count,
            "see_all": see_all_links,
        }

    def _get_recent_sessions(self, user: User) -> list[Session]:
        runs = Run.objects.only(
            "id",
            "session_id",
            "status",
            "started_at",
            "finished_at",
            "merge_request_iid",
            "merge_request_web_url",
            "cost_usd",
            "agent_model",
            "created_at",
        ).order_by("-created_at", "-id")
        return list(
            Session.objects
            .listed_for(user, mine=True)
            .select_related("user", "scheduled_job")
            .prefetch_related(Prefetch("runs", queryset=runs))
            .order_by("-last_active_at")[:RECENT_SESSIONS_LIMIT]
        )

    def _get_ways_to_use(self) -> dict:
        is_github = codebase_settings.CLIENT == GitPlatform.GITHUB
        mcp_url = self.request.build_absolute_uri("/mcp/")
        return {
            "platform_name": "GitHub" if is_github else "GitLab",
            "mr_term": "pull request" if is_github else "merge request",
            "platform_icon": "github" if is_github else "gitlab",
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


def _period(request, default: str) -> str:
    period = request.GET.get("period", default)
    return period if period in insights.PERIOD_DAYS else default


def _periods() -> list[dict]:
    return [{"key": key, "label": label} for key, label, _days in insights.PERIOD_CHOICES]


class InsightsView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/insights.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        period = _period(self.request, insights.DEFAULT_PERIOD)
        window = insights.window_for(period)
        previous = window.previous()
        context.update({
            "periods": _periods(),
            "current_period": period,
            "window": window,
            "previous_window": previous,
            "activity": insights.activity(user, window, previous, chart=True),
            "running": running_jobs_count(self.request, user),
            "breakdown": insights.session_breakdown(user, window, previous, sparks=True),
            "velocity": insights.velocity(user, window, chart=True),
        })
        return context
