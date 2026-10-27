import logging
from datetime import date, datetime, timedelta
from typing import NamedTuple

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.db.models import (
    Avg,
    Case,
    Count,
    DurationField,
    Exists,
    ExpressionWrapper,
    F,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.timezone import localdate
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from notifications.choices import EnvelopeStatus
from notifications.outcomes import CANT_RUN, CI_STILL_FAILING, ENVELOPE_OUTCOMES, Outcome
from sessions.filters import SCOPE_ALL, SCOPE_MINE, SessionFilter
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState

from accounts.models import APIKey, User
from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.conf import settings as codebase_settings
from codebase.models import MergeMetric
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL
from dashboard.context_processors import running_jobs_count
from schedules.models import Frequency, ScheduledJob

logger = logging.getLogger("daiv.dashboard")


def homepage(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "dashboard/homepage.html")


PERIOD_CHOICES = [
    ("today", "Today", 0),
    ("7d", "7 days", 7),
    ("30d", "30 days", 30),
    ("90d", "90 days", 90),
    ("all", "All time", None),
]
PERIOD_DAYS = {key: days for key, _, days in PERIOD_CHOICES}
DEFAULT_PERIOD = "7d"


# Hues follow sessions/_origin_badge.html, so a segment matches the badges on the list it opens.
ORIGIN_SEGMENT_CSS = {
    SessionOrigin.ISSUE_WEBHOOK: "bg-amber-500/50",
    SessionOrigin.MR_WEBHOOK: "bg-cyan-500/50",
    SessionOrigin.PIPELINE_WEBHOOK: "bg-orange-500/40",
    SessionOrigin.MCP_JOB: "bg-blue-500/50",
    SessionOrigin.SCHEDULE: "bg-violet-500/40",
    SessionOrigin.API_JOB: "bg-indigo-500/50",
    SessionOrigin.UI_JOB: "bg-emerald-500/50",
    SessionOrigin.CHAT: "bg-teal-500/40",
    SessionOrigin.DELEGATED_JOB: "bg-fuchsia-500/40",
    SessionOrigin.DELEGATION_RESUME: "bg-pink-500/40",
}


def _period_params(cutoff_date: date | None) -> dict[str, str]:
    return {"date_from": cutoff_date.isoformat()} if cutoff_date is not None else {}


def _session_list_url(params: dict[str, str]) -> str:
    return f"{reverse('session_list')}?{urlencode({'scope': SCOPE_ALL, **params})}"


def _raw_pct(numerator: int, denominator: int) -> int | None:
    """Return a ratio as a rounded integer percentage, or None when the denominator is zero."""
    return round(numerator / denominator * 100) if denominator else None


def _format_pct(numerator: int, denominator: int) -> str:
    """Format a ratio as a rounded percentage string, or a dash when the denominator is zero."""
    raw = _raw_pct(numerator, denominator)
    return f"{raw}%" if raw is not None else "—"


def _success_rate_css(pct: int | None) -> str:
    """Text color for a success-rate percentage; the plain strong text when there is none."""
    if pct is None:
        return "text-text-strong"
    if pct >= 90:
        return "text-status-clear"
    if pct >= 75:
        return "text-status-found"
    return "text-status-fail"


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
        context["my_sessions_url"] = f"{reverse('session_list')}?{urlencode({'scope': SCOPE_MINE})}"
        context["ways"] = self._get_ways_to_use()

        period = self.request.GET.get("period", DEFAULT_PERIOD)
        if period not in PERIOD_DAYS:
            period = DEFAULT_PERIOD
        days = PERIOD_DAYS[period]
        cutoff_date = localdate() - timedelta(days=max(days - 1, 0)) if days is not None else None

        context["activity"] = self._get_activity_data(cutoff_date, user)
        context["active_api_keys"] = APIKey.objects.filter(user=user, revoked=False).count()
        context["periods"] = [{"key": key, "label": label} for key, label, _ in PERIOD_CHOICES]
        context["current_period"] = period
        context["velocity"] = self._get_velocity_data(cutoff_date) if user.is_admin else None
        context["active_schedules"] = ScheduledJob.objects.filter(user=user, is_enabled=True).count()
        if user.is_admin:
            context["total_users"] = User.objects.count()

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

    def _get_activity_data(self, cutoff_date: date | None, user: User) -> dict:
        visible = Run.objects.visible_to(user)
        activities = visible.filter(created_at__date__gte=cutoff_date) if cutoff_date is not None else visible

        successful = Q(status=RunStatus.SUCCESSFUL)
        failed = Q(status=RunStatus.FAILED)
        duration_expr = ExpressionWrapper(F("finished_at") - F("started_at"), output_field=DurationField())

        stats = activities.aggregate(
            total=Count("id"),
            successful=Count("id", filter=successful),
            failed_count=Count("id", filter=failed),
            code_changes=Count("id", filter=successful & Q(code_changes=True)),
            avg_duration=Avg(duration_expr, filter=successful),
        )

        # "Running now" is global (not period-filtered); share the request-memoized helper
        # with the ``nav`` context processor so the sidebar badge and dashboard card resolve
        # to a single query per render.
        running_count = running_jobs_count(self.request, user)

        successful_count = stats["successful"]
        failed_count = stats["failed_count"]
        code_changes_count = stats["code_changes"]
        avg_duration = stats["avg_duration"]

        return {
            "total": stats["total"],
            "running": running_count,
            "success_rate": _format_pct(successful_count, successful_count + failed_count),
            "success_rate_css": _success_rate_css(_raw_pct(successful_count, successful_count + failed_count)),
            "failed": failed_count,
            "code_changes": code_changes_count,
            "code_changes_pct": _format_pct(code_changes_count, successful_count),
            "avg_duration_s": avg_duration.total_seconds() if avg_duration is not None else None,
            **self._get_session_breakdown(cutoff_date, user),
        }

    def _get_session_breakdown(self, cutoff_date: date | None, user: User) -> dict:
        # Counted through the list's own queryset and filterset, so each count equals the length
        # of the list its link opens.
        period = _period_params(cutoff_date)
        sessions = SessionFilter(period, queryset=Session.objects.listed_for(user)).qs
        rows = list(
            sessions
            .order_by()
            .values("origin")
            .annotate(n=Count("pk"), failed=Count("pk", filter=Q(latest_run_status=RunStatus.FAILED)))
        )
        origin_counts = {row["origin"]: row["n"] for row in rows}

        total = sum(origin_counts.values())
        segments = [
            {
                "label": SessionOrigin(origin).label,
                "value": n,
                "pct": round(n / total * 100, 1),
                "css": ORIGIN_SEGMENT_CSS[origin],
                "url": _session_list_url({**period, "trigger": origin}),
            }
            for origin in SessionOrigin.values
            if (n := origin_counts.get(origin))
        ]
        return {
            "sessions_total": total,
            "sessions_url": _session_list_url(period),
            "segments": segments,
            "sessions_failed": sum(row["failed"] for row in rows),
            "sessions_failed_url": _session_list_url({**period, "status": RunStatus.FAILED}),
        }

    def _get_velocity_data(self, cutoff_date: date | None) -> dict | None:
        merges = MergeMetric.objects.all()
        if cutoff_date is not None:
            merges = merges.filter(merged_at__date__gte=cutoff_date)

        stats = merges.aggregate(
            total=Count("id"),
            total_added=Sum("lines_added", default=0),
            total_removed=Sum("lines_removed", default=0),
            daiv_merges=Count("id", filter=Q(daiv_commits__gt=0)),
            total_commits_sum=Sum("total_commits", default=0),
            daiv_commits_sum=Sum("daiv_commits", default=0),
        )

        if not stats["total"]:
            return None

        total_merges = stats["total"]
        daiv_merges = stats["daiv_merges"]
        human_merges = total_merges - daiv_merges
        total_commits = stats["total_commits_sum"]
        daiv_commits = stats["daiv_commits_sum"]
        human_commits = max(0, total_commits - daiv_commits)
        max_lines = max(stats["total_added"], stats["total_removed"], 1)

        return {
            "total_merges": total_merges,
            "lines_added": stats["total_added"],
            "lines_removed": stats["total_removed"],
            "net_lines": stats["total_added"] - stats["total_removed"],
            "daiv_merges_pct": _format_pct(daiv_merges, total_merges),
            "daiv_merges_pct_raw": min(_raw_pct(daiv_merges, total_merges) or 0, 100),
            "daiv_merges": daiv_merges,
            "human_merges": human_merges,
            "daiv_commits_pct": _format_pct(daiv_commits, total_commits),
            "daiv_commits_pct_raw": min(_raw_pct(daiv_commits, total_commits) or 0, 100),
            "daiv_commits": daiv_commits,
            "human_commits": human_commits,
            "lines_added_pct": round(stats["total_added"] / max_lines * 100, 1),
            "lines_removed_pct": round(stats["total_removed"] / max_lines * 100, 1),
        }
