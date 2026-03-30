import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError
from django.db.models import Count, Q
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from accounts.forms import APIKeyCreateForm
from accounts.models import APIKey

logger = logging.getLogger(__name__)

ISSUE_TASK_PATH = "codebase.tasks.address_issue_task"
MR_TASK_PATH = "codebase.tasks.address_mr_comments_task"
TASK_PATHS = (ISSUE_TASK_PATH, MR_TASK_PATH)

PERIOD_CHOICES = [("7d", "7 days", 7), ("30d", "30 days", 30), ("90d", "90 days", 90), ("all", "All time", None)]
PERIOD_DAYS = {key: days for key, _, days in PERIOD_CHOICES}
DEFAULT_PERIOD = "30d"


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        period = self.request.GET.get("period", DEFAULT_PERIOD)
        if period not in PERIOD_DAYS:
            period = DEFAULT_PERIOD
        days = PERIOD_DAYS[period]

        tasks = DBTaskResult.objects.filter(task_path__in=TASK_PATHS)
        if days is not None:
            tasks = tasks.filter(enqueued_at__gte=timezone.now() - timedelta(days=days))

        successful = Q(status=TaskResultStatus.SUCCESSFUL)
        code_changes = Q(return_value__code_changes=True)
        stats = tasks.aggregate(
            total=Count("id"),
            successful=Count("id", filter=successful),
            issues=Count("id", filter=successful & code_changes & Q(task_path=ISSUE_TASK_PATH)),
            mrs=Count("id", filter=successful & code_changes & Q(task_path=MR_TASK_PATH)),
        )
        active_api_keys = APIKey.objects.filter(user=self.request.user, revoked=False).count()

        total = stats["total"]
        context["counters"] = [
            {"label": "Jobs processed", "value": total},
            {"label": "Success rate", "value": f"{round(stats['successful'] / total * 100)}%" if total else "—"},
            {"label": "Issues resolved", "value": stats["issues"]},
            {"label": "MRs assisted", "value": stats["mrs"]},
            {"label": "Active API keys", "value": active_api_keys},
        ]
        context["periods"] = [{"key": key, "label": label} for key, label, _ in PERIOD_CHOICES]
        context["current_period"] = period
        return context


class APIKeyListView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/api_keys.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["api_keys"] = APIKey.objects.filter(user=self.request.user).order_by("revoked", "-created")
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
