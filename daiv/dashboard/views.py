from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.views.generic import TemplateView

from accounts.models import APIKey, User
from dashboard import insights
from dashboard.context_processors import running_jobs_count
from schedules.models import ScheduledJob


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

        user = self.request.user
        context["activity"] = {
            **insights.activity(user, window),
            "running": running_jobs_count(self.request, user),
            **insights.session_breakdown(user, window),
        }
        context["active_api_keys"] = APIKey.objects.filter(user=user, revoked=False).count()
        context["periods"] = _periods()
        context["current_period"] = period
        context["velocity"] = _home_velocity(user, window) if user.is_admin else None
        context["active_schedules"] = ScheduledJob.objects.filter(user=user, is_enabled=True).count()
        if user.is_admin:
            context["total_users"] = User.objects.count()

        return context


def _home_velocity(user: User, window: insights.Window) -> dict | None:
    velocity = insights.velocity(user, window)
    if velocity is None:
        return None
    total_merges, daiv_merges = velocity["total_merges"], velocity["daiv_merges"]
    total_commits, daiv_commits = velocity["total_commits"], velocity["daiv_commits"]
    lines_added, lines_removed = velocity["lines_added"], velocity["lines_removed"]
    max_lines = max(lines_added, lines_removed, 1)
    return {
        **velocity,
        "net_lines": lines_added - lines_removed,
        "daiv_merges_pct_raw": min(insights.pct(daiv_merges, total_merges) or 0, 100),
        "human_merges": total_merges - daiv_merges,
        "daiv_commits_pct_raw": min(insights.pct(daiv_commits, total_commits) or 0, 100),
        "human_commits": max(0, total_commits - daiv_commits),
        "lines_added_pct": round(lines_added / max_lines * 100, 1),
        "lines_removed_pct": round(lines_removed / max_lines * 100, 1),
    }


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
