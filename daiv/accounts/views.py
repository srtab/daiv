import logging
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db import IntegrityError
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q, Sum
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.timezone import localdate
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, TemplateView, UpdateView

from django_filters.views import FilterView
from sessions.models import Run, RunStatus, SessionOrigin

from accounts.context_processors import running_jobs_count
from accounts.emails import send_welcome_email
from accounts.filters import UserFilter
from accounts.forms import APIKeyCreateForm, UserCreateForm, UserUpdateForm
from accounts.mixins import AdminRequiredMixin, BreadcrumbMixin
from accounts.models import APIKey, User
from codebase.models import MergeMetric
from core.utils import is_htmx
from schedules.models import ScheduledJob

logger = logging.getLogger(__name__)


def homepage(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "accounts/homepage.html")


PICKER_USERS_LIMIT = 10
PICKER_USERS_MIN_QUERY = 2


class UserPickerView(LoginRequiredMixin, TemplateView):
    """HTMX fragment: up to ``PICKER_USERS_LIMIT`` active users matching ``?q=``.

    Excludes the requesting user and any ids passed in ``?exclude=`` (CSV).
    """

    template_name = "accounts/_user_picker_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get("q", "").strip()
        if len(query) < PICKER_USERS_MIN_QUERY:
            context["users"] = []
            return context

        exclude_ids: set[int] = {self.request.user.pk}
        for part in self.request.GET.get("exclude", "").split(","):
            stripped = part.strip()
            if stripped.isdigit():
                exclude_ids.add(int(stripped))

        context["users"] = (
            User.objects
            .filter(is_active=True)
            .filter(Q(username__icontains=query) | Q(email__icontains=query) | Q(name__icontains=query))
            .exclude(pk__in=exclude_ids)
            .only("pk", "username", "name", "email")
            .order_by("username")[:PICKER_USERS_LIMIT]
        )
        return context


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


def _format_duration(td: timedelta | None) -> str:
    """Format a timedelta as a compact human-readable string, or a dash when None."""
    if td is None:
        return "—"
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        return "—"
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _resolve_period(request, default: str = DEFAULT_PERIOD) -> tuple[str, date | None]:
    """Resolve the stateless ``?period=`` querystring to a ``(period_key, cutoff_date)`` pair.

    Falls back to ``default`` for a missing/invalid value — the personal console uses the module
    ``DEFAULT_PERIOD`` ("today"), while the org ``ManagerLensView`` passes ``default="all"`` because
    org velocity is cumulative. ``cutoff_date`` is ``None`` for the "all time" period, today's date
    for the zero-day "today" period, and ``today - days`` otherwise. Shared by both console surfaces
    so they honour the same range semantics with no persisted state.
    """
    period = request.GET.get("period", default)
    if period not in PERIOD_DAYS:
        period = default
    days = PERIOD_DAYS[period]
    cutoff_date = localdate() - timedelta(days=days) if days is not None else None
    if days == 0:
        cutoff_date = localdate()
    return period, cutoff_date


def get_velocity_data(cutoff_date: date | None) -> dict | None:
    """Aggregate org-wide code-velocity + DAIV-attribution from ``MergeMetric``.

    Org-wide by design — ``MergeMetric.objects.all()`` (never user-scoped, never derived from
    ``RunEnvelope`` per AD-10); an optional ``cutoff_date`` restricts to merges on/after that date.
    Returns ``None`` when there are zero matching rows so the caller can render an honest cold-load
    state instead of a misleading zero reading. Single source of truth for the Manager Lens.
    """
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


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/dashboard.html"

    def get_template_names(self) -> list[str]:
        # HTMX requests get just the console-body fragment (three region sections);
        # a normal GET renders the full shell. Mirrors ``SessionListView`` so later
        # stories' region partials refresh in place.
        if is_htmx(self.request):
            return ["accounts/_console_body.html"]
        return ["accounts/dashboard.html"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        period, cutoff_date = _resolve_period(self.request)

        # Personal-by-default (AC1): the console default carries NO org/aggregate content for
        # anyone — admin OR member. Org velocity / total_users now live ONLY in the Manager Lens
        # (``ManagerLensView``); every read here is personal-scoped via ``visible_to`` / ``user=``.
        user = self.request.user
        context["activity"] = self._get_activity_data(cutoff_date, user)
        context["active_api_keys"] = APIKey.objects.filter(user=user, revoked=False).count()
        context["periods"] = [{"key": key, "label": label} for key, label, _ in PERIOD_CHOICES]
        context["current_period"] = period
        context["active_schedules"] = ScheduledJob.objects.filter(user=user, is_enabled=True).count()

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

        return {
            "total": total,
            "running": running_count,
            "success_rate": _format_pct(successful_count, successful_count + failed_count),
            "success_rate_raw": _raw_pct(successful_count, successful_count + failed_count),
            "failed": failed_count,
            "code_changes": code_changes_count,
            "code_changes_pct": _format_pct(code_changes_count, successful_count),
            "avg_duration": _format_duration(stats["avg_duration"]),
            "activity_url": sessions_url,
            "segments": segments,
        }


class ManagerLensView(AdminRequiredMixin, TemplateView):
    """Admin-only org-impact surface — the relocated Code-Velocity / DAIV-attribution content.

    ``AdminRequiredMixin`` raises ``PermissionDenied`` (→ HTTP 403) for a logged-in non-admin, so a
    forged direct request is denied server-side, not merely hidden (AC6). Reuses the stateless
    ``?period=`` idiom and the shared module-level ``get_velocity_data`` computation (no duplicated
    query). Nothing about the current surface is persisted (AC4) — the Lens is simply a distinct
    route from the personal console, so a fresh landing is always ``/dashboard/``.
    """

    template_name = "accounts/manager_lens.html"

    def get_template_names(self) -> list[str]:
        # HTMX (the top-bar toggle) swaps in the body fragment; a normal GET / no-JS deep link
        # renders the full shell. Mirrors ``SessionListView`` / ``DashboardView``.
        if is_htmx(self.request):
            return ["accounts/_manager_lens.html"]
        return ["accounts/manager_lens.html"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        _, cutoff_date = _resolve_period(self.request, default="all")

        # Org-scoped by design — this is the ONE place org velocity / total_users render. The Lens
        # defaults to the cumulative "all" window (not the personal console's "today"): org velocity
        # is cumulative, so a fresh visit shows real historical impact instead of an empty view.
        # ``get_velocity_data`` then returns ``None`` only for a genuinely-empty org (zero
        # ``MergeMetric`` rows ever) — the honest cold-load skeleton, never a "DAIV did nothing"
        # zero (AC5). No range switcher on the Lens yet — that arrives with Epic 3.
        context["velocity"] = get_velocity_data(cutoff_date)
        context["total_users"] = User.objects.count()

        return context


class APIKeyListView(LoginRequiredMixin, ListView):
    model = APIKey
    template_name = "accounts/api_keys.html"
    context_object_name = "api_keys"
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset().order_by("revoked", "-created")
        if self.request.user.is_admin:
            return qs.select_related("user")
        return qs.filter(user=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["new_key"] = self.request.session.pop("new_api_key", None)
        context["form"] = APIKeyCreateForm()
        context["is_admin"] = self.request.user.is_admin
        return context


class APIKeyCreateView(LoginRequiredMixin, View):
    def post(self, request):
        form = APIKeyCreateForm(request.POST)
        if not form.is_valid():
            for error in form.errors.values():
                messages.error(request, error[0])
            return redirect("api_keys")

        try:
            key_generator = APIKey.objects.key_generator
            key, prefix, hashed_key = key_generator.generate()
            APIKey.objects.create(
                user=request.user, name=form.cleaned_data["name"], prefix=prefix, hashed_key=hashed_key
            )
        except IntegrityError:
            messages.error(request, "Failed to create API key due to a conflict. Please try again.")
            return redirect("api_keys")
        except Exception:
            logger.exception("Unexpected error creating API key for user %s", request.user.pk)
            messages.error(request, "An unexpected error occurred. Please try again.")
            return redirect("api_keys")

        request.session["new_api_key"] = key
        messages.success(request, f"API key '{form.cleaned_data['name']}' created.")
        return redirect("api_keys")


class APIKeyRevokeView(LoginRequiredMixin, View):
    def post(self, request, pk):
        qs = APIKey.objects.all() if request.user.is_admin else APIKey.objects.filter(user=request.user)
        api_key = qs.filter(pk=pk).first()
        if api_key is None:
            messages.error(request, "API key not found.")
        elif api_key.revoked:
            messages.info(request, f"API key '{api_key.name}' was already revoked.")
        else:
            api_key.revoked = True
            api_key.save(update_fields=["revoked"])
            messages.success(request, f"API key '{api_key.name}' revoked.")
        return redirect("api_keys")


# ---------------------------------------------------------------------------
# User management views (admin only)
# ---------------------------------------------------------------------------


class UserListView(AdminRequiredMixin, FilterView):
    model = User
    filterset_class = UserFilter
    template_name = "accounts/users.html"
    context_object_name = "users"
    ordering = ["-date_joined"]
    paginate_by = 25
    # Invalid URL params (e.g. ?role=bogus) drop silently instead of blanking the list.
    strict = False

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context["filter"].form
        cleaned = form.cleaned_data if form.is_valid() else {}
        context["search_query"] = cleaned.get("q") or ""
        context["current_role"] = cleaned.get("role") or ""
        return context


class UserCreateView(BreadcrumbMixin, AdminRequiredMixin, CreateView):
    model = User
    form_class = UserCreateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("user_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        assert isinstance(self.object, User)
        login_url = self.request.build_absolute_uri(reverse("account_login"))
        email_sent = send_welcome_email(self.object, login_url)
        if email_sent:
            messages.success(self.request, f"User '{self.object.email}' created. A welcome email has been sent.")
        else:
            messages.warning(
                self.request,
                f"User '{self.object.email}' created, but the welcome email could not be sent."
                " Please notify the user manually.",
            )
        return response

    def get_breadcrumbs(self):
        return [{"label": "Users", "url": reverse("user_list")}, {"label": "New user", "url": None}]


class UserUpdateView(BreadcrumbMixin, SuccessMessageMixin, AdminRequiredMixin, UpdateView):
    model = User
    form_class = UserUpdateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("user_list")
    success_message = "User '%(email)s' updated."

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["requesting_user"] = self.request.user
        return kwargs

    def get_breadcrumbs(self):
        return [{"label": "Users", "url": reverse("user_list")}, {"label": self.object.email, "url": None}]


class UserDeleteView(BreadcrumbMixin, SuccessMessageMixin, AdminRequiredMixin, DeleteView):
    model = User
    template_name = "accounts/user_confirm_delete.html"
    success_url = reverse_lazy("user_list")
    success_message = "User deleted."

    def post(self, request, *args, **kwargs):
        user = self.get_object()
        if user.pk == request.user.pk:
            messages.error(request, "You cannot delete your own account.")
            return redirect("user_list")
        if user.is_last_active_admin():
            messages.error(request, "Cannot delete the last admin. Promote another user first.")
            return redirect("user_list")
        return super().post(request, *args, **kwargs)

    def get_success_message(self, cleaned_data: dict) -> str:
        return f"User '{self.object.email}' deleted."

    def get_breadcrumbs(self):
        return [
            {"label": "Users", "url": reverse("user_list")},
            {"label": self.object.email, "url": reverse("user_update", args=[self.object.pk])},
            {"label": "Delete", "url": None},
        ]
