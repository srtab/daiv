import zoneinfo

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from schedules.forms import ScheduledJobCreateForm, ScheduledJobUpdateForm
from schedules.models import ScheduledJob

COMMON_TIMEZONES = sorted(tz for tz in zoneinfo.available_timezones() if "/" in tz and not tz.startswith("Etc/"))


class _ScheduleOwnerMixin:
    """Scopes querysets to the current user for non-admin users and provides timezone context."""

    def get_queryset(self):
        qs = super().get_queryset()
        if not self.request.user.is_admin:
            qs = qs.filter(user=self.request.user)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["timezones"] = COMMON_TIMEZONES
        return context


class ScheduleListView(LoginRequiredMixin, ListView):
    model = ScheduledJob
    template_name = "schedules/schedule_list.html"
    context_object_name = "schedules"
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.is_admin:
            return qs.select_related("user")
        return qs.filter(user=self.request.user)


class ScheduleCreateView(_ScheduleOwnerMixin, LoginRequiredMixin, CreateView):
    model = ScheduledJob
    form_class = ScheduledJobCreateForm
    template_name = "schedules/schedule_form.html"
    success_url = reverse_lazy("schedule_list")

    def form_valid(self, form):
        form.instance.user = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, f"Schedule '{self.object.name}' created.")
        return response


class ScheduleUpdateView(_ScheduleOwnerMixin, LoginRequiredMixin, UpdateView):
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
