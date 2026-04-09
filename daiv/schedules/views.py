from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from schedules.forms import ScheduledJobCreateForm, ScheduledJobUpdateForm
from schedules.models import ScheduledJob


class _ScheduleOwnerMixin:
    """Scopes querysets to the current user."""

    def get_queryset(self):
        return ScheduledJob.objects.by_owner(self.request.user)


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


class ScheduleCreateView(_ScheduleOwnerMixin, SuccessMessageMixin, LoginRequiredMixin, CreateView):
    model = ScheduledJob
    form_class = ScheduledJobCreateForm
    template_name = "schedules/schedule_form.html"
    success_url = reverse_lazy("schedule_list")
    success_message = "Schedule '%(name)s' created."

    def form_valid(self, form):
        form.instance.user = self.request.user
        return super().form_valid(form)


class ScheduleUpdateView(_ScheduleOwnerMixin, SuccessMessageMixin, LoginRequiredMixin, UpdateView):
    model = ScheduledJob
    form_class = ScheduledJobUpdateForm
    template_name = "schedules/schedule_form.html"
    success_url = reverse_lazy("schedule_list")
    success_message = "Schedule '%(name)s' updated."


class ScheduleDeleteView(_ScheduleOwnerMixin, SuccessMessageMixin, LoginRequiredMixin, DeleteView):
    model = ScheduledJob
    template_name = "schedules/schedule_confirm_delete.html"
    success_url = reverse_lazy("schedule_list")
    success_message = "Schedule deleted."

    def get_success_message(self, cleaned_data: dict) -> str:
        return f"Schedule '{self.object.name}' deleted."
