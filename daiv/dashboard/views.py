from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import redirect
from django.views.generic import TemplateView

from skills.models import SkillInvocation
from skills.pages import SkillPageMixin

from core.mixins import AdminRequiredMixin
from dashboard import home, insights, landing
from dashboard.comparisons import CHECKED, COMPARISONS, COMPARISONS_BY_SLUG
from dashboard.context_processors import running_jobs_count
from dashboard.sidebar import SESSIONS_PAGE, sidebar_lists
from dashboard.skill_usage import RECENT_RUNS, match_runs, skill_usage


class LandingPageView(TemplateView):
    def get_context_data(self, **kwargs):
        return super().get_context_data(
            quickstart_url=landing.QUICKSTART_URL, deployment_url=landing.DEPLOYMENT_URL, **kwargs
        )


class HomepageView(LandingPageView):
    template_name = "dashboard/homepage.html"
    extra_context = {"faqs": landing.FAQS, "comparisons": COMPARISONS}

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("dashboard")
        return super().get(request, *args, **kwargs)


class ComparisonView(LandingPageView):
    template_name = "dashboard/comparison.html"

    def get_context_data(self, **kwargs):
        try:
            comparison = COMPARISONS_BY_SLUG[kwargs["slug"]]
        except KeyError:
            raise Http404("No comparison with that name") from None
        return super().get_context_data(
            comparison=comparison,
            checked=CHECKED,
            others=[other for other in COMPARISONS if other is not comparison],
            **kwargs,
        )


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


class SkillUsageView(AdminRequiredMixin, SkillPageMixin, TemplateView):
    """The skill page's Usage tab."""

    template_name = "dashboard/skill_usage.html"
    skill_tab = "usage"

    def get_context_data(self, **kwargs):
        page = self.skill_page
        recent = list(
            SkillInvocation.objects.filter(name=page.name, source=page.source).order_by("-created")[:RECENT_RUNS]
        )
        return super().get_context_data(
            usage=skill_usage(page.name, page.source), recent_runs=match_runs(recent), **kwargs
        )


class SidebarListsView(LoginRequiredMixin, TemplateView):
    """The sidebar's two lists alone, for the sidebar to swap in when runs start or finish."""

    template_name = "dashboard/_sidebar_lists.html"
    MAX_LIMIT = 80

    def get_context_data(self, **kwargs):
        try:
            limit = int(self.request.GET.get("limit", SESSIONS_PAGE))
        except ValueError:
            limit = SESSIONS_PAGE
        limit = min(max(limit, SESSIONS_PAGE), self.MAX_LIMIT)
        lists = sidebar_lists(self.request.user, limit=limit, viewing=self.request.GET.get("viewing", ""))
        return super().get_context_data(nav_sidebar=lists, **kwargs)
