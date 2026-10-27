from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q, Sum
from django.shortcuts import redirect, render
from django.views.generic import TemplateView

from accounts.models import APIKey, User
from codebase.models import MergeMetric
from dashboard import insights
from dashboard.context_processors import running_jobs_count
from schedules.models import ScheduledJob

if TYPE_CHECKING:
    from datetime import date


def homepage(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "dashboard/homepage.html")


HOME_DEFAULT_PERIOD = "today"


def _period(request, default: str) -> str:
    period = request.GET.get("period", default)
    return period if period in insights.PERIOD_DAYS else default


def _periods() -> list[dict]:
    return [{"key": key, "label": label} for key, label, _days in insights.PERIOD_CHOICES]


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        period = _period(self.request, HOME_DEFAULT_PERIOD)
        window = insights.window_for(period)
        cutoff_date = window.start

        user = self.request.user
        context["activity"] = {
            **insights.activity(user, window, trends=False),
            "running": running_jobs_count(self.request, user),
            **insights.session_breakdown(user, window, trends=False),
        }
        context["active_api_keys"] = APIKey.objects.filter(user=user, revoked=False).count()
        context["periods"] = _periods()
        context["current_period"] = period
        context["velocity"] = self._get_velocity_data(cutoff_date) if user.is_admin else None
        context["active_schedules"] = ScheduledJob.objects.filter(user=user, is_enabled=True).count()
        if user.is_admin:
            context["total_users"] = User.objects.count()

        return context

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
            "daiv_merges_pct": insights.format_pct(daiv_merges, total_merges),
            "daiv_merges_pct_raw": min(insights.pct(daiv_merges, total_merges) or 0, 100),
            "daiv_merges": daiv_merges,
            "human_merges": human_merges,
            "daiv_commits_pct": insights.format_pct(daiv_commits, total_commits),
            "daiv_commits_pct_raw": min(insights.pct(daiv_commits, total_commits) or 0, 100),
            "daiv_commits": daiv_commits,
            "human_commits": human_commits,
            "lines_added_pct": round(stats["total_added"] / max_lines * 100, 1),
            "lines_removed_pct": round(stats["total_removed"] / max_lines * 100, 1),
        }


class InsightsView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/insights.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        period = _period(self.request, insights.DEFAULT_PERIOD)
        window = insights.window_for(period)
        context.update({
            "periods": _periods(),
            "current_period": period,
            "window": window,
            "previous_window": window.previous(),
            "activity": insights.activity(user, window),
            "running": running_jobs_count(self.request, user),
            "breakdown": insights.session_breakdown(user, window),
            "velocity": insights.velocity(user, window),
        })
        return context
