from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import redirect
from django.views.generic import TemplateView

from skills.models import SkillInvocation
from skills.pages import SkillPageMixin

from core.mixins import AdminRequiredMixin
from dashboard import home, insights, landing
from dashboard.comparisons import CHECKED, COMPARISONS, COMPARISONS_BY_SLUG
from dashboard.conf import settings as dashboard_settings
from dashboard.context_processors import running_jobs_count
from dashboard.sidebar import SESSIONS_PAGE, sidebar_lists
from dashboard.skill_usage import RECENT_RUNS, match_runs, skill_usage


class LandingPageView(TemplateView):
    def get_context_data(self, **kwargs):
        return super().get_context_data(
            quickstart_url=landing.QUICKSTART_URL, deployment_url=landing.DEPLOYMENT_URL, umami=self.umami(), **kwargs
        )

    def umami(self) -> dict[str, str] | None:
        """Umami's tracker settings, for a signed-out visitor on an install that set a website ID."""
        if dashboard_settings.UMAMI_WEBSITE_ID is None or self.request.user.is_authenticated:
            return None
        return {
            "script_url": str(dashboard_settings.UMAMI_SCRIPT_URL),
            "website_id": str(dashboard_settings.UMAMI_WEBSITE_ID),
        }


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
        has_sessions = home.has_sessions(user)
        context.update({
            "inbox": home.inbox(user),
            "has_sessions": has_sessions,
            "your_week": home.your_week(user) if has_sessions else None,
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

    def get_context_data(self, **kwargs):
        query = self.request.GET
        try:
            limit = int(query.get("limit", SESSIONS_PAGE))
        except ValueError:
            limit = SESSIONS_PAGE
        lists = sidebar_lists(self.request.user, limit=limit, viewing=query.get("viewing", ""))
        # The page's section, not this endpoint's: the rows highlight the section the sidebar is on.
        return super().get_context_data(nav_sidebar=lists, nav_active_section=query.get("section", ""), **kwargs)
