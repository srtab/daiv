import zoneinfo

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from schedules.forms import ScheduledJobCreateForm, ScheduledJobUpdateForm
from schedules.models import ScheduledJob, ScheduledJobRun

COMMON_TIMEZONES = sorted(tz for tz in zoneinfo.available_timezones() if "/" in tz and not tz.startswith("Etc/"))


class _ScheduleOwnerMixin:
    """Scopes querysets to the current user."""

    def get_queryset(self):
        return ScheduledJob.objects.by_owner(self.request.user)


class _TimezoneContextMixin:
    """Provides timezone list for form views."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["timezones"] = COMMON_TIMEZONES
        return context


class ScheduleListView(_ScheduleOwnerMixin, LoginRequiredMixin, ListView):
    model = ScheduledJob
    template_name = "schedules/schedule_list.html"
    context_object_name = "schedules"
    paginate_by = 25

    def get_queryset(self):
        qs = ScheduledJob.objects.by_owner(self.request.user)
        if self.request.user.is_admin:
            qs = qs.select_related("user")
        return qs


class ScheduleCreateView(_TimezoneContextMixin, _ScheduleOwnerMixin, LoginRequiredMixin, CreateView):
    model = ScheduledJob
    form_class = ScheduledJobCreateForm
    template_name = "schedules/schedule_form.html"
    success_url = reverse_lazy("schedule_list")

    def form_valid(self, form):
        form.instance.user = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, f"Schedule '{self.object.name}' created.")
        return response


class ScheduleUpdateView(_TimezoneContextMixin, _ScheduleOwnerMixin, LoginRequiredMixin, UpdateView):
    model = ScheduledJob
    form_class = ScheduledJobUpdateForm
    template_name = "schedules/schedule_form.html"
    success_url = reverse_lazy("schedule_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Schedule '{self.object.name}' updated.")
        return response


class ScheduleDeleteView(_ScheduleOwnerMixin, LoginRequiredMixin, DeleteView):
    model = ScheduledJob
    template_name = "schedules/schedule_confirm_delete.html"
    success_url = reverse_lazy("schedule_list")

    def form_valid(self, form):
        assert isinstance(self.object, ScheduledJob)
        name = self.object.name
        response = super().form_valid(form)
        messages.success(self.request, f"Schedule '{name}' deleted.")
        return response


class _ScheduleRunMixin:
    """Provides user-scoped lookup of the parent ScheduledJob for run views."""

    schedule_pk_url_kwarg: str = "pk"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(scheduled_job_id=self.kwargs[self.schedule_pk_url_kwarg])
            .select_related("task_result")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["schedule"] = get_object_or_404(
            ScheduledJob.objects.by_owner(self.request.user), pk=self.kwargs[self.schedule_pk_url_kwarg]
        )
        return context


class ScheduleRunListView(_ScheduleRunMixin, LoginRequiredMixin, ListView):
    model = ScheduledJobRun
    template_name = "schedules/run_list.html"
    context_object_name = "runs"
    paginate_by = 25


class ScheduleRunDetailView(_ScheduleRunMixin, LoginRequiredMixin, DetailView):
    model = ScheduledJobRun
    template_name = "schedules/run_detail.html"
    context_object_name = "run"
    schedule_pk_url_kwarg = "schedule_pk"
