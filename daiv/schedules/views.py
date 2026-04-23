import json
import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from activity.models import TriggerType
from activity.services import RepoTarget, submit_batch_runs

from accounts.mixins import BreadcrumbMixin
from schedules.forms import ScheduledJobCreateForm, ScheduledJobUpdateForm
from schedules.models import ScheduledJob

logger = logging.getLogger("daiv.schedules")


class _ScheduleOwnerMixin:
    """Scopes querysets to the current user."""

    def get_queryset(self):
        return ScheduledJob.objects.by_owner(self.request.user)


def _subscriber_initial_json(schedule) -> str:
    """Serialize a schedule's current subscribers for the Alpine picker."""
    if schedule is None:
        return "[]"
    rows = [{"id": u.pk, "username": u.username, "name": u.name, "email": u.email} for u in schedule.subscribers.all()]
    return json.dumps(rows)


class ScheduleListView(_ScheduleOwnerMixin, LoginRequiredMixin, ListView):
    model = ScheduledJob
    template_name = "schedules/schedule_list.html"
    context_object_name = "schedules"
    paginate_by = 25

    def get_queryset(self):
        return ScheduledJob.objects.by_owner(self.request.user).select_related("user").prefetch_related("subscribers")


class ScheduleCreateView(BreadcrumbMixin, _ScheduleOwnerMixin, SuccessMessageMixin, LoginRequiredMixin, CreateView):
    model = ScheduledJob
    form_class = ScheduledJobCreateForm
    template_name = "schedules/schedule_form.html"
    success_url = reverse_lazy("schedule_list")
    success_message = "Schedule '%(name)s' created."
    breadcrumbs = [{"label": "Schedules", "url": reverse_lazy("schedule_list")}, {"label": "New schedule", "url": None}]

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["owner"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["subscriber_initial_json"] = "[]"
        return context

    def form_valid(self, form):
        form.instance.user = self.request.user
        return super().form_valid(form)


class ScheduleUpdateView(BreadcrumbMixin, _ScheduleOwnerMixin, SuccessMessageMixin, LoginRequiredMixin, UpdateView):
    model = ScheduledJob
    form_class = ScheduledJobUpdateForm
    template_name = "schedules/schedule_form.html"
    success_url = reverse_lazy("schedule_list")
    success_message = "Schedule '%(name)s' updated."

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["owner"] = self.object.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["subscriber_initial_json"] = _subscriber_initial_json(self.object)
        return context

    def get_breadcrumbs(self):
        return [
            {"label": "Schedules", "url": reverse("schedule_list")},
            {"label": f'"{self.object.name}"', "url": None},
        ]


class ScheduleToggleView(_ScheduleOwnerMixin, LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        with transaction.atomic():
            schedule = get_object_or_404(self.get_queryset().select_related("user").select_for_update(), pk=pk)
            schedule.is_enabled = not schedule.is_enabled
            if schedule.is_enabled:
                try:
                    schedule.compute_next_run()
                except ValueError, TypeError:
                    logger.exception(
                        "Cannot compute next run for schedule pk=%d (%s); cron config may be invalid",
                        schedule.pk,
                        schedule.name,
                    )
                    schedule.refresh_from_db()
                    html = render_to_string(
                        "schedules/_schedule_row.html", {"schedule": schedule, "user": request.user}, request=request
                    )
                    response = HttpResponse(html, content_type="text/html")
                    response["HX-Reswap"] = "outerHTML"
                    response["HX-Trigger"] = "schedule-toggle-error"
                    return response
            else:
                schedule.next_run_at = None
            schedule.save(update_fields=["is_enabled", "next_run_at", "modified"])
        html = render_to_string(
            "schedules/_schedule_row.html", {"schedule": schedule, "user": request.user}, request=request
        )
        return HttpResponse(html, content_type="text/html")


class ScheduleRunNowView(_ScheduleOwnerMixin, LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):

        schedule = get_object_or_404(self.get_queryset(), pk=pk)
        repos = [RepoTarget(repo_id=r["repo_id"], ref=r["ref"]) for r in schedule.repos]
        try:
            result = submit_batch_runs(
                user=request.user,
                prompt=schedule.prompt,
                repos=repos,
                use_max=schedule.use_max,
                notify_on=None,
                trigger_type=TriggerType.SCHEDULE,
                scheduled_job=schedule,
            )
        except Exception:
            logger.exception("Failed to enqueue run-now for schedule pk=%d (%s)", schedule.pk, schedule.name)
            messages.error(request, f"Failed to trigger schedule '{schedule.name}'. Please try again.")
            return redirect("schedule_list")

        if result.failed:
            failed_ids = ", ".join(f.repo_id for f in result.failed)
            messages.warning(request, f"Schedule '{schedule.name}' triggered with failures: {failed_ids}.")
        else:
            messages.success(request, f"Schedule '{schedule.name}' triggered successfully.")

        if len(result.activities) == 1 and not result.failed:
            return redirect("activity_detail", pk=result.activities[0].pk)
        if result.activities:
            return redirect(reverse("activity_list") + f"?batch={result.batch_id}")
        return redirect("schedule_list")


class ScheduleUnsubscribeView(LoginRequiredMixin, View):
    """Let a subscriber remove themselves from a schedule."""

    http_method_names = ["post"]

    def post(self, request, pk):
        schedule = get_object_or_404(ScheduledJob, pk=pk)
        if not schedule.subscribers.filter(pk=request.user.pk).exists():
            raise Http404
        schedule.subscribers.remove(request.user)
        messages.success(request, f"You are no longer subscribed to '{schedule.name}'.")
        next_url = request.POST.get("next", "")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return redirect(next_url)
        return redirect("activity_list")


class ScheduleDeleteView(BreadcrumbMixin, _ScheduleOwnerMixin, SuccessMessageMixin, LoginRequiredMixin, DeleteView):
    model = ScheduledJob
    template_name = "schedules/schedule_confirm_delete.html"
    success_url = reverse_lazy("schedule_list")
    success_message = "Schedule deleted."

    def get_success_message(self, cleaned_data: dict) -> str:
        return f"Schedule '{self.object.name}' deleted."

    def get_breadcrumbs(self):
        return [
            {"label": "Schedules", "url": reverse("schedule_list")},
            {"label": f'"{self.object.name}"', "url": reverse("schedule_update", args=[self.object.pk])},
            {"label": "Delete", "url": None},
        ]
