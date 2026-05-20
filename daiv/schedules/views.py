import json
import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db import transaction
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from activity.models import TriggerType
from activity.services import RepoTarget, submit_batch_runs
from sandbox_envs.services import env_picker_context, resolve_repo_envs

from accounts.mixins import AdminRequiredMixin, BreadcrumbMixin
from accounts.templatetags.avatar_tags import user_color_index, user_initials
from schedules.forms import ScheduledJobCreateForm, ScheduledJobUpdateForm, ScheduleTemplateForm
from schedules.models import ScheduledJob, ScheduleTemplate

logger = logging.getLogger("daiv.schedules")


class _ScheduleOwnerMixin:
    """Scopes querysets to the current user."""

    def get_queryset(self):
        return ScheduledJob.objects.by_owner(self.request.user)


def _subscriber_initial_json(schedule) -> str:
    """Serialize a schedule's current subscribers for the Alpine picker.

    ``initials`` and ``color_index`` are precomputed server-side so the chip
    avatars render identically to ``_avatar.html`` without a JS hashing helper.
    """
    if schedule is None:
        return "[]"
    rows = [
        {
            "id": u.pk,
            "username": u.username,
            "name": u.name,
            "email": u.email,
            "initials": user_initials(u),
            "color_index": user_color_index(u),
        }
        for u in schedule.subscribers.all()
    ]
    return json.dumps(rows)


def _template_picker_payload() -> list[dict]:
    """Build the gallery drawer's JSON payload, most-used templates first."""
    qs = (
        ScheduleTemplate.objects
        .only(*ScheduleTemplate.PICKER_FIELDS)
        .annotate(usage_count=Count("schedules"))
        .order_by("-usage_count", "name")
    )
    return [t.to_picker_dict() for t in qs]


class ScheduleListView(_ScheduleOwnerMixin, LoginRequiredMixin, ListView):
    model = ScheduledJob
    template_name = "schedules/schedule_list.html"
    context_object_name = "schedules"
    paginate_by = 25

    def get_queryset(self):
        return ScheduledJob.objects.by_owner(self.request.user).select_related("user").prefetch_related("subscribers")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["schedule_templates"] = _template_picker_payload()
        return context


class ScheduleCreateView(BreadcrumbMixin, _ScheduleOwnerMixin, SuccessMessageMixin, LoginRequiredMixin, CreateView):
    model = ScheduledJob
    form_class = ScheduledJobCreateForm
    template_name = "schedules/schedule_form.html"
    success_url = reverse_lazy("schedule_list")
    success_message = "Schedule '%(name)s' created."
    breadcrumbs = [{"label": "Schedules", "url": reverse_lazy("schedule_list")}, {"label": "New schedule", "url": None}]

    def _get_template(self) -> ScheduleTemplate | None:
        pk = self.request.GET.get("template")
        if not pk:
            return None
        try:
            pk_int = int(pk)
        except ValueError:
            return None
        return ScheduleTemplate.objects.filter(pk=pk_int).first()

    def _get_source_schedule(self) -> ScheduledJob | None:
        pk = self.request.GET.get("from")
        if not pk:
            return None
        try:
            pk_int = int(pk)
        except ValueError:
            return None
        return self.get_queryset().filter(pk=pk_int).first()

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["owner"] = self.request.user
        kwargs["user"] = self.request.user
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        tpl = self._get_template()
        if tpl is not None:
            initial.update(tpl.to_schedule_kwargs())
        source = self._get_source_schedule()
        if source is not None:
            initial.update(source.to_schedule_kwargs())
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["subscriber_initial_json"] = "[]"
        context["schedule_templates"] = _template_picker_payload()
        tpl = self._get_template()
        context["selected_template_id"] = str(tpl.pk) if tpl is not None else ""
        context.update(env_picker_context(context["form"]))
        return context

    def form_valid(self, form):
        form.instance.user = self.request.user
        tpl = self._get_template()
        if tpl is not None:
            form.instance.source_template = tpl
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
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["subscriber_initial_json"] = _subscriber_initial_json(self.object)
        context.update(env_picker_context(context["form"]))
        return context

    def get_breadcrumbs(self):
        return [
            {"label": "Schedules", "url": reverse("schedule_list")},
            {"label": f'"{self.object.name}"', "url": None},
        ]


class ScheduleToggleView(_ScheduleOwnerMixin, LoginRequiredMixin, View):
    http_method_names = ["post"]

    def _render_error_response(self, request, schedule) -> HttpResponse:
        """Render the current schedule row and signal HTMX to roll back the toggle attempt."""
        schedule.refresh_from_db()
        html = render_to_string(
            "schedules/_schedule_row.html", {"schedule": schedule, "user": request.user}, request=request
        )
        response = HttpResponse(html, content_type="text/html")
        response["HX-Reswap"] = "outerHTML"
        response["HX-Trigger"] = "schedule-toggle-error"
        return response

    def post(self, request, pk):
        with transaction.atomic():
            schedule = get_object_or_404(self.get_queryset().select_related("user").select_for_update(), pk=pk)
            schedule.is_enabled = not schedule.is_enabled
            if schedule.is_enabled:
                if schedule.is_fired_one_off:
                    logger.info(
                        "Refusing to re-enable fired one-off schedule pk=%d (%s); use Duplicate instead.",
                        schedule.pk,
                        schedule.name,
                    )
                    return self._render_error_response(request, schedule)
                try:
                    schedule.compute_next_run()
                except ValueError, TypeError:
                    logger.exception(
                        "Cannot compute next run for schedule pk=%d (%s); cron config may be invalid",
                        schedule.pk,
                        schedule.name,
                    )
                    return self._render_error_response(request, schedule)
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
        # Resolve against the schedule owner, not request.user, so admin "Run now" picks the
        # owner's USER envs — same semantics as the cron dispatcher.
        repos = resolve_repo_envs(
            user=schedule.user,
            repos=repos,
            explicit_env_id=str(schedule.sandbox_environment_id) if schedule.sandbox_environment_id else None,
        )
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


class ScheduleTemplateListView(BreadcrumbMixin, AdminRequiredMixin, ListView):
    model = ScheduleTemplate
    template_name = "schedules/template_list.html"
    context_object_name = "templates"
    paginate_by = 25
    breadcrumbs = [{"label": "Schedules", "url": reverse_lazy("schedule_list")}, {"label": "Templates", "url": None}]

    def get_queryset(self):
        return ScheduleTemplate.objects.only(*ScheduleTemplate.PICKER_FIELDS)


class ScheduleTemplateCreateView(BreadcrumbMixin, AdminRequiredMixin, SuccessMessageMixin, CreateView):
    model = ScheduleTemplate
    form_class = ScheduleTemplateForm
    template_name = "schedules/template_form.html"
    success_url = reverse_lazy("schedule_template_list")
    success_message = "Template '%(name)s' created."
    breadcrumbs = [
        {"label": "Schedules", "url": reverse_lazy("schedule_list")},
        {"label": "Templates", "url": reverse_lazy("schedule_template_list")},
        {"label": "New template", "url": None},
    ]

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        return super().form_valid(form)


class ScheduleTemplateUpdateView(BreadcrumbMixin, AdminRequiredMixin, SuccessMessageMixin, UpdateView):
    model = ScheduleTemplate
    form_class = ScheduleTemplateForm
    template_name = "schedules/template_form.html"
    success_url = reverse_lazy("schedule_template_list")
    success_message = "Template '%(name)s' updated."

    def get_breadcrumbs(self):
        return [
            {"label": "Schedules", "url": reverse("schedule_list")},
            {"label": "Templates", "url": reverse("schedule_template_list")},
            {"label": f'"{self.object.name}"', "url": None},
        ]


class ScheduleDuplicateView(_ScheduleOwnerMixin, LoginRequiredMixin, View):
    """POST-only redirect to ``schedule_create?from=<pk>``.

    Duplication is a write-leaning action triggered from a dropdown button, so POST
    avoids link prefetchers, browser caching, and accidental GETs from crawlers.
    Cross-user access is enforced here via ``_ScheduleOwnerMixin`` (returns 404);
    ``ScheduleCreateView`` re-validates ownership when reading the ``from`` param.
    """

    http_method_names = ["post"]

    def post(self, request, pk):
        get_object_or_404(self.get_queryset(), pk=pk)
        query = urlencode({"from": pk})
        return redirect(f"{reverse('schedule_create')}?{query}")


class ScheduleTemplateDeleteView(BreadcrumbMixin, AdminRequiredMixin, SuccessMessageMixin, DeleteView):
    model = ScheduleTemplate
    template_name = "schedules/template_confirm_delete.html"
    success_url = reverse_lazy("schedule_template_list")

    def get_success_message(self, cleaned_data: dict) -> str:
        return f"Template '{self.object.name}' deleted."

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        next_url = self.request.GET.get("next", "")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={self.request.get_host()}):
            context["cancel_url"] = next_url
        else:
            context["cancel_url"] = reverse("schedule_template_update", args=[self.object.pk])
        return context

    def get_breadcrumbs(self):
        return [
            {"label": "Schedules", "url": reverse("schedule_list")},
            {"label": "Templates", "url": reverse("schedule_template_list")},
            {"label": f'"{self.object.name}"', "url": reverse("schedule_template_update", args=[self.object.pk])},
            {"label": "Delete", "url": None},
        ]
