import csv

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import urlencode
from django.views.generic import TemplateView, View

from sessions.filters import SCOPE_ALL, SCOPE_CHOICES, SCOPE_MINE
from sessions.spend_ledger import COLUMNS, SpendLedger
from skills.models import SkillInvocation
from skills.pages import SkillPageMixin

from core.api.throttling import SpendExportThrottle
from core.mixins import AdminRequiredMixin
from dashboard import home, insights, landing, spend
from dashboard.comparisons import CHECKED, COMPARISONS, COMPARISONS_BY_SLUG
from dashboard.conf import settings as dashboard_settings
from dashboard.sidebar import SESSIONS_PAGE, Sidebar
from dashboard.skill_usage import RECENT_RUNS, match_runs, skill_usage


class LandingPageView(TemplateView):
    def get_context_data(self, **kwargs):
        return super().get_context_data(
            quickstart_url=landing.QUICKSTART_URL,
            deployment_url=landing.DEPLOYMENT_URL,
            social_links=landing.SOCIAL_LINKS,
            umami=self.umami(),
            **kwargs,
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
        has_started_runs = home.has_started_runs(user)
        context.update({
            "inbox": home.inbox(user),
            "has_started_runs": has_started_runs,
            "your_week": home.your_week(user) if has_started_runs else None,
            "ways": home.ways_to_use(),
        })
        return context


def _period(request, default: str = insights.DEFAULT_PERIOD) -> str:
    period = request.GET.get("period", default)
    return period if period in insights.PERIOD_DAYS else default


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
            "breakdown": insights.session_breakdown(user, window, previous),
            "velocity": insights.velocity(user, window),
        })
        return context


class SpendScopeMixin:
    """The Spend page's period and scope: administrators open on everyone's spend, members on their own."""

    default_period = "30d"

    def spend_period(self) -> str:
        return _period(self.request, self.default_period)

    def spend_scope(self) -> str:
        default = SCOPE_ALL if self.request.user.is_admin else SCOPE_MINE
        scope = self.request.GET.get("scope", default)
        return scope if scope in dict(SCOPE_CHOICES) else default


class InsightsSpendView(LoginRequiredMixin, SpendScopeMixin, TemplateView):
    """What DAIV spent, by whom, where and on what; members open on their own spend."""

    template_name = "dashboard/insights_spend.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        period = self.spend_period()
        scope = self.spend_scope()
        window = insights.window_for(period)
        previous = window.previous()
        report = spend.SpendReport(user, window, previous, scope)
        totals = report.totals()
        context.update({
            "periods": [{"key": key, "label": label} for key, label, _days in insights.PERIOD_CHOICES],
            "scopes": [{"key": key, "label": label} for key, label in SCOPE_CHOICES],
            "current_period": period,
            "current_scope": scope,
            "window": window,
            "previous_window": previous,
            "totals": totals,
            "export_url": f"{reverse('insights_spend_export')}?{urlencode({'period': period, 'scope': scope})}",
            "counting_docs_url": f"{landing.DOCS_ROOT}/reference/monitoring/#usage-and-cost",
            "cache_good": spend.CACHE_GOOD,
            "cache_fair": spend.CACHE_FAIR,
        })
        if not totals["empty"]:
            coverage = report.coverage()
            context.update({
                "chart": report.chart(),
                "breakdowns": report.breakdowns(),
                "top_sessions": report.top_sessions(),
                "sessions_by_cost_url": report.sessions_by_cost_url(),
                "coverage": coverage,
                "cost_floor": bool(totals["unpriced_tokens"] or coverage["unrecorded_runs"]),
            })
        return context


class SpendExportView(LoginRequiredMixin, SpendScopeMixin, View):
    """The Spend page's records as CSV, one row per record, for the page's period and scope."""

    FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

    class _Echo:
        def write(self, value: str) -> str:
            return value

    def get(self, request, *args, **kwargs):
        throttle = SpendExportThrottle()
        if not throttle.allow_request(request):
            return HttpResponse(status=429, headers={"Retry-After": str(int(throttle.wait() or 60))})
        window = insights.window_for(self.spend_period())
        records = SpendLedger.rows(request.user, self.spend_scope(), window.start, window.end)
        start = window.start.isoformat() if window.start is not None else "all"
        response = StreamingHttpResponse(self._csv_lines(records), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="daiv-spend-{start}-{window.end.isoformat()}.csv"'
        return response

    @classmethod
    async def _csv_lines(cls, records):
        writer = csv.writer(cls._Echo())
        yield writer.writerow(COLUMNS)
        async for record in records.aiterator(chunk_size=2000):
            row = SpendLedger.row(record)
            yield writer.writerow([cls._csv_value(row[column]) for column in COLUMNS])

    @classmethod
    def _csv_value(cls, value) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str) and value.startswith(cls.FORMULA_PREFIXES):
            # A leading quote keeps a spreadsheet from running a person's or repository's name as a formula.
            return f"'{value}"
        return str(value)


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
        lists = Sidebar(self.request.user, viewing=query.get("viewing", "")).lists(limit=limit)
        # The page's section, not this endpoint's: the rows highlight the section the sidebar is on.
        return super().get_context_data(nav_sidebar=lists, nav_active_section=query.get("section", ""), **kwargs)
