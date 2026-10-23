from datetime import date, timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q, Sum
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.timezone import localdate
from django.views.generic import TemplateView

from sessions.models import Run, RunStatus, SessionOrigin

from accounts.models import APIKey, User
from codebase.models import MergeMetric
from dashboard.context_processors import running_jobs_count
from schedules.models import ScheduledJob


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
DEFAULT_PERIOD = "today"


def _raw_pct(numerator: int, denominator: int) -> int | None:
    """Return a ratio as a rounded integer percentage, or None when the denominator is zero."""
    return round(numerator / denominator * 100) if denominator else None


def _format_pct(numerator: int, denominator: int) -> str:
    """Format a ratio as a rounded percentage string, or a dash when the denominator is zero."""
    raw = _raw_pct(numerator, denominator)
    return f"{raw}%" if raw is not None else "—"


def _success_rate_css(pct: int | None) -> str:
    """Tailwind text color for a success-rate percentage; plain white when there is none."""
    if pct is None:
        return "text-white"
    if pct >= 90:
        return "text-emerald-400"
    if pct >= 75:
        return "text-amber-400"
    return "text-red-400"


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        period = self.request.GET.get("period", DEFAULT_PERIOD)
        if period not in PERIOD_DAYS:
            period = DEFAULT_PERIOD
        days = PERIOD_DAYS[period]
        cutoff_date = localdate() - timedelta(days=max(days - 1, 0)) if days is not None else None

        user = self.request.user
        context["activity"] = self._get_activity_data(cutoff_date, user)
        context["active_api_keys"] = APIKey.objects.filter(user=user, revoked=False).count()
        context["periods"] = [{"key": key, "label": label} for key, label, _ in PERIOD_CHOICES]
        context["current_period"] = period
        context["velocity"] = self._get_velocity_data(cutoff_date) if user.is_admin else None
        context["active_schedules"] = ScheduledJob.objects.filter(user=user, is_enabled=True).count()
        if user.is_admin:
            context["total_users"] = User.objects.count()

        return context

    def _get_activity_data(self, cutoff_date: date | None, user: User) -> dict:
        visible = Run.objects.visible_to(user)
        activities = visible.filter(created_at__date__gte=cutoff_date) if cutoff_date is not None else visible

        successful = Q(status=RunStatus.SUCCESSFUL)
        failed = Q(status=RunStatus.FAILED)
        issue_trigger = Q(trigger_type=SessionOrigin.ISSUE_WEBHOOK)
        mr_trigger = Q(trigger_type=SessionOrigin.MR_WEBHOOK)
        mcp_trigger = Q(trigger_type=SessionOrigin.MCP_JOB)
        schedule_trigger = Q(trigger_type=SessionOrigin.SCHEDULE)
        api_trigger = Q(trigger_type=SessionOrigin.API_JOB)
        chat_trigger = Q(trigger_type=SessionOrigin.CHAT)
        duration_expr = ExpressionWrapper(F("finished_at") - F("started_at"), output_field=DurationField())

        stats = activities.aggregate(
            total=Count("id"),
            successful=Count("id", filter=successful),
            failed_count=Count("id", filter=failed),
            issues=Count("id", filter=issue_trigger & ~failed),
            mrs=Count("id", filter=mr_trigger & ~failed),
            mcp_jobs=Count("id", filter=mcp_trigger & ~failed),
            scheduled=Count("id", filter=schedule_trigger & ~failed),
            api_jobs=Count("id", filter=api_trigger & ~failed),
            chat_jobs=Count("id", filter=chat_trigger & ~failed),
            code_changes=Count("id", filter=successful & Q(code_changes=True)),
            avg_duration=Avg(duration_expr, filter=successful),
        )

        # "Running now" is global (not period-filtered); share the request-memoized helper
        # with the ``nav`` context processor so the sidebar badge and dashboard card resolve
        # to a single query per render.
        running_count = running_jobs_count(self.request, user)

        total = stats["total"]
        successful_count = stats["successful"]
        failed_count = stats["failed_count"]
        issues_count = stats["issues"]
        mrs_count = stats["mrs"]
        mcp_jobs_count = stats["mcp_jobs"]
        scheduled_count = stats["scheduled"]
        api_jobs_count = stats["api_jobs"]
        chat_jobs_count = stats["chat_jobs"]
        sessions_url = reverse("session_list")

        # Non-overlapping segments for the breakdown bar.
        # Trigger types are mutually exclusive, so the trigger-keyed segments never overlap.
        # Each segment excludes failed; "Other" absorbs the remainder (e.g. UI jobs).
        other_count = max(
            0,
            total
            - issues_count
            - mrs_count
            - mcp_jobs_count
            - scheduled_count
            - api_jobs_count
            - chat_jobs_count
            - failed_count,
        )
        raw_segments = [
            ("Issues", issues_count, "bg-amber-500/50", f"{sessions_url}?trigger={SessionOrigin.ISSUE_WEBHOOK}"),
            ("MR/PR", mrs_count, "bg-cyan-500/50", f"{sessions_url}?trigger={SessionOrigin.MR_WEBHOOK}"),
            ("MCP Job", mcp_jobs_count, "bg-indigo-500/50", f"{sessions_url}?trigger={SessionOrigin.MCP_JOB}"),
            ("Scheduled", scheduled_count, "bg-violet-500/40", f"{sessions_url}?trigger={SessionOrigin.SCHEDULE}"),
            ("API", api_jobs_count, "bg-emerald-500/50", f"{sessions_url}?trigger={SessionOrigin.API_JOB}"),
            ("Chat", chat_jobs_count, "bg-sky-500/40", f"{sessions_url}?trigger={SessionOrigin.CHAT}"),
            ("Other", other_count, "bg-gray-500/30", None),
            ("Failed", failed_count, "bg-red-500/40", f"{sessions_url}?status={RunStatus.FAILED}"),
        ]
        segments = []
        for label, value, css, url in raw_segments:
            if value <= 0:
                continue
            segments.append({
                "label": label,
                "value": value,
                "pct": round(value / total * 100, 1) if total else 0,
                "css": css,
                "url": url,
            })

        code_changes_count = stats["code_changes"]
        avg_duration = stats["avg_duration"]

        return {
            "total": total,
            "running": running_count,
            "success_rate": _format_pct(successful_count, successful_count + failed_count),
            "success_rate_css": _success_rate_css(_raw_pct(successful_count, successful_count + failed_count)),
            "failed": failed_count,
            "code_changes": code_changes_count,
            "code_changes_pct": _format_pct(code_changes_count, successful_count),
            "avg_duration_s": avg_duration.total_seconds() if avg_duration is not None else None,
            "activity_url": sessions_url,
            "segments": segments,
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
