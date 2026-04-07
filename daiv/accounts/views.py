import logging
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError
from django.db.models import Count, Q, Sum
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.timezone import localdate
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, TemplateView, UpdateView

from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult
from schedules.models import ScheduledJob

from accounts.emails import send_welcome_email
from accounts.forms import APIKeyCreateForm, UserCreateForm, UserUpdateForm
from accounts.mixins import AdminRequiredMixin
from accounts.models import APIKey, Role, User
from codebase.models import MergeMetric

logger = logging.getLogger(__name__)


def homepage(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "accounts/homepage.html")


ISSUE_TASK_PATH = "codebase.tasks.address_issue_task"
MR_TASK_PATH = "codebase.tasks.address_mr_comments_task"
TASK_PATHS = (ISSUE_TASK_PATH, MR_TASK_PATH)

PERIOD_CHOICES = [("7d", "7 days", 7), ("30d", "30 days", 30), ("90d", "90 days", 90), ("all", "All time", None)]
PERIOD_DAYS = {key: days for key, _, days in PERIOD_CHOICES}
DEFAULT_PERIOD = "30d"


def _format_pct(numerator: int, denominator: int) -> str:
    """Format a ratio as a rounded percentage string, or a dash when the denominator is zero."""
    return f"{round(numerator / denominator * 100)}%" if denominator else "—"


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        period = self.request.GET.get("period", DEFAULT_PERIOD)
        if period not in PERIOD_DAYS:
            period = DEFAULT_PERIOD
        days = PERIOD_DAYS[period]
        today = localdate()
        cutoff_date = today - timedelta(days=days) if days is not None else None

        context["counters"] = self._get_activity_counters(cutoff_date, today)
        context["active_api_keys"] = APIKey.objects.filter(user=self.request.user, revoked=False).count()
        context["periods"] = [{"key": key, "label": label} for key, label, _ in PERIOD_CHOICES]
        context["current_period"] = period
        context["merge_counters"] = self._get_merge_counters(cutoff_date, today)
        context["active_schedules"] = ScheduledJob.objects.filter(user=self.request.user, is_enabled=True).count()
        if self.request.user.is_admin:
            context["total_users"] = User.objects.count()

        return context

    def _get_activity_counters(self, cutoff_date: date | None, today: date) -> list[dict]:
        tasks = DBTaskResult.objects.filter(task_path__in=TASK_PATHS)
        if cutoff_date is not None:
            tasks = tasks.filter(enqueued_at__date__gte=cutoff_date)

        successful = Q(status=TaskResultStatus.SUCCESSFUL)
        code_changes = Q(return_value__code_changes=True)
        today_q = Q(enqueued_at__date=today)
        stats = tasks.aggregate(
            total=Count("id"),
            successful=Count("id", filter=successful),
            issues=Count("id", filter=successful & code_changes & Q(task_path=ISSUE_TASK_PATH)),
            mrs=Count("id", filter=successful & code_changes & Q(task_path=MR_TASK_PATH)),
            today_total=Count("id", filter=today_q),
            today_issues=Count("id", filter=today_q & successful & code_changes & Q(task_path=ISSUE_TASK_PATH)),
            today_mrs=Count("id", filter=today_q & successful & code_changes & Q(task_path=MR_TASK_PATH)),
        )

        total = stats["total"]
        return [
            {"label": "Jobs processed", "value": total, "today": stats["today_total"]},
            {"label": "Success rate", "value": _format_pct(stats["successful"], total)},
            {"label": "Issues resolved", "value": stats["issues"], "today": stats["today_issues"]},
            {"label": "MR reviews addressed", "value": stats["mrs"], "today": stats["today_mrs"]},
        ]

    def _get_merge_counters(self, cutoff_date: date | None, today: date) -> list[dict]:
        merges = MergeMetric.objects.all()
        if cutoff_date is not None:
            merges = merges.filter(merged_at__date__gte=cutoff_date)

        today_q = Q(merged_at__date=today)
        stats = merges.aggregate(
            total=Count("id"),
            total_added=Sum("lines_added", default=0),
            total_removed=Sum("lines_removed", default=0),
            daiv_added=Sum("daiv_lines_added", default=0),
            daiv_removed=Sum("daiv_lines_removed", default=0),
            total_commits_sum=Sum("total_commits", default=0),
            daiv_commits_sum=Sum("daiv_commits", default=0),
            today_total=Count("id", filter=today_q),
            today_added=Sum("lines_added", default=0, filter=today_q),
            today_removed=Sum("lines_removed", default=0, filter=today_q),
        )

        total_lines = stats["total_added"] + stats["total_removed"]
        daiv_lines = stats["daiv_added"] + stats["daiv_removed"]
        total_commits = stats["total_commits_sum"]

        return [
            {
                "label": "Total merges",
                "value": stats["total"],
                "tooltip": "Number of MRs/PRs merged into default branches.",
                "today": stats["today_total"],
            },
            {
                "label": "Lines added",
                "value": stats["total_added"],
                "tooltip": "Total lines added across all merged MRs/PRs.",
                "today": stats["today_added"],
            },
            {
                "label": "Lines removed",
                "value": stats["total_removed"],
                "tooltip": "Total lines removed across all merged MRs/PRs.",
                "today": stats["today_removed"],
            },
            {
                "label": "DAIV contribution",
                "value": _format_pct(daiv_lines, total_lines),
                "tooltip": "Percentage of total lines (added + removed) authored by DAIV, based on commit authorship.",
                "plain": True,
            },
            {
                "label": "DAIV commit share",
                "value": _format_pct(stats["daiv_commits_sum"], total_commits),
                "tooltip": "Percentage of total commits authored by DAIV across all merged MRs/PRs.",
                "plain": True,
            },
        ]


class APIKeyListView(LoginRequiredMixin, ListView):
    model = APIKey
    template_name = "accounts/api_keys.html"
    context_object_name = "api_keys"
    paginate_by = 25

    def get_queryset(self):
        return super().get_queryset().filter(user=self.request.user).order_by("revoked", "-created")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["new_key"] = self.request.session.pop("new_api_key", None)
        context["form"] = APIKeyCreateForm()
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
        api_key = APIKey.objects.filter(pk=pk, user=request.user).first()
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


class UserUpdateView(AdminRequiredMixin, UpdateView):
    model = User
    form_class = UserUpdateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("user_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["requesting_user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        assert isinstance(self.object, User)
        messages.success(self.request, f"User '{self.object.email}' updated.")
        return response


class UserDeleteView(AdminRequiredMixin, DeleteView):
    model = User
    template_name = "accounts/user_confirm_delete.html"
    success_url = reverse_lazy("user_list")

    def post(self, request, *args, **kwargs):
        user = self.get_object()
        if user.pk == request.user.pk:
            messages.error(request, "You cannot delete your own account.")
            return redirect("user_list")
        if user.is_last_active_admin():
            messages.error(request, "Cannot delete the last admin. Promote another user first.")
            return redirect("user_list")
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        assert isinstance(self.object, User)
        messages.success(self.request, f"User '{self.object.email}' deleted.")
        return super().form_valid(form)
