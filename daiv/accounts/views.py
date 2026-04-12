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

from activity.models import Activity, ActivityStatus, TriggerType

from accounts.emails import send_welcome_email
from accounts.forms import APIKeyCreateForm, UserCreateForm, UserUpdateForm
from accounts.mixins import AdminRequiredMixin
from accounts.models import APIKey, Role, User
from codebase.models import MergeMetric
from schedules.models import ScheduledJob

logger = logging.getLogger(__name__)


def homepage(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "accounts/homepage.html")


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


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        period = self.request.GET.get("period", DEFAULT_PERIOD)
        if period not in PERIOD_DAYS:
            period = DEFAULT_PERIOD
        days = PERIOD_DAYS[period]
        cutoff_date = localdate() - timedelta(days=days) if days is not None else None
        if days == 0:
            cutoff_date = localdate()

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
        owned = Activity.objects.by_owner(user)
        activities = owned.filter(created_at__date__gte=cutoff_date) if cutoff_date is not None else owned

        successful = Q(status=ActivityStatus.SUCCESSFUL)
        failed = Q(status=ActivityStatus.FAILED)
        issue_trigger = Q(trigger_type=TriggerType.ISSUE_WEBHOOK)
        mr_trigger = Q(trigger_type=TriggerType.MR_WEBHOOK)
        mcp_trigger = Q(trigger_type=TriggerType.MCP_JOB)
        schedule_trigger = Q(trigger_type=TriggerType.SCHEDULE)
        duration_expr = ExpressionWrapper(F("finished_at") - F("started_at"), output_field=DurationField())

        stats = activities.aggregate(
            total=Count("id"),
            successful=Count("id", filter=successful),
            failed_count=Count("id", filter=failed),
            issues=Count("id", filter=issue_trigger & ~failed),
            mrs=Count("id", filter=mr_trigger & ~failed),
            mcp_jobs=Count("id", filter=mcp_trigger & ~failed),
            scheduled=Count("id", filter=schedule_trigger & ~failed),
            code_changes=Count("id", filter=successful & Q(code_changes=True)),
            avg_duration=Avg(duration_expr, filter=successful),
        )

        running_count = owned.filter(status=ActivityStatus.RUNNING).count()

        total = stats["total"]
        successful_count = stats["successful"]
        failed_count = stats["failed_count"]
        issues_count = stats["issues"]
        mrs_count = stats["mrs"]
        mcp_jobs_count = stats["mcp_jobs"]
        scheduled_count = stats["scheduled"]
        activity_url = reverse("activity_list")

        # Non-overlapping segments for the breakdown bar.
        # Trigger types are mutually exclusive, so issues/mrs/mcp/scheduled never overlap.
        # Each segment excludes failed; "Other" absorbs the remainder (e.g. API jobs).
        other_count = max(0, total - issues_count - mrs_count - mcp_jobs_count - scheduled_count - failed_count)
        raw_segments = [
            ("Issues", issues_count, "bg-amber-500/50", f"{activity_url}?trigger={TriggerType.ISSUE_WEBHOOK}"),
            ("MR/PR", mrs_count, "bg-cyan-500/50", f"{activity_url}?trigger={TriggerType.MR_WEBHOOK}"),
            ("MCP Job", mcp_jobs_count, "bg-indigo-500/50", f"{activity_url}?trigger={TriggerType.MCP_JOB}"),
            ("Scheduled", scheduled_count, "bg-violet-500/40", f"{activity_url}?trigger={TriggerType.SCHEDULE}"),
            ("Other", other_count, "bg-gray-500/30", None),
            ("Failed", failed_count, "bg-red-500/40", f"{activity_url}?status={ActivityStatus.FAILED}"),
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
            "activity_url": activity_url,
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


class UserListView(AdminRequiredMixin, ListView):
    model = User
    template_name = "accounts/users.html"
    context_object_name = "users"
    ordering = ["-date_joined"]
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(email__icontains=q))
        role = self.request.GET.get("role", "").strip()
        if role in {Role.ADMIN, Role.MEMBER}:
            qs = qs.filter(role=role)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_query"] = self.request.GET.get("q", "")
        context["current_role"] = self.request.GET.get("role", "")
        return context


class UserCreateView(AdminRequiredMixin, CreateView):
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


class UserUpdateView(SuccessMessageMixin, AdminRequiredMixin, UpdateView):
    model = User
    form_class = UserUpdateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("user_list")
    success_message = "User '%(email)s' updated."

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["requesting_user"] = self.request.user
        return kwargs


class UserDeleteView(SuccessMessageMixin, AdminRequiredMixin, DeleteView):
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
