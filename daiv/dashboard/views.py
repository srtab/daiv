from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.views.generic import TemplateView

from dashboard import home, insights
from dashboard.context_processors import running_jobs_count


def homepage(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "dashboard/homepage.html")


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        recent_sessions = home.recent_sessions(user)
        context.update({
            "inbox": home.inbox(user),
            "recent_sessions": recent_sessions,
            "recent_in_flight_ids": home.in_flight_run_ids(recent_sessions),
            "my_sessions_url": home.my_sessions_url(),
            "your_week": home.your_week(user) if recent_sessions else None,
            "ways": home.ways_to_use(),
        })
        return context


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
