from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from django_filters.views import FilterView

from accounts.mixins import AdminRequiredMixin, BreadcrumbMixin
from core.site_settings import site_settings
from memory.filters import MemoryObservationFilter
from memory.models import MemoryObservation, ObservationCategory, ObservationStatus, RepositoryMemory
from memory.tasks import consolidate_memory_task


class MemoryListView(LoginRequiredMixin, TemplateView):
    template_name = "memory/list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        obs_rows = {
            row["repo_id"]: row
            for row in MemoryObservation.objects.values("repo_id").annotate(
                total=Count("pk"), pending=Count("pk", filter=Q(status=ObservationStatus.PENDING))
            )
        }
        mem_rows = {mem.repo_id: mem for mem in RepositoryMemory.objects.all()}

        repos = []
        for repo_id in sorted(set(obs_rows) | set(mem_rows)):
            obs = obs_rows.get(repo_id)
            mem = mem_rows.get(repo_id)
            repos.append({
                "repo_id": repo_id,
                "total": obs["total"] if obs else 0,
                "pending": obs["pending"] if obs else 0,
                "has_document": bool(mem and mem.content.strip()),
                "last_consolidated_at": mem.last_consolidated_at if mem else None,
            })

        ctx["repos"] = repos
        ctx["memory_enabled"] = site_settings.memory_enabled
        return ctx


class MemoryDetailView(BreadcrumbMixin, LoginRequiredMixin, FilterView):
    template_name = "memory/detail.html"
    filterset_class = MemoryObservationFilter
    paginate_by = 50
    # Preserve pre-django-filter UX: an invalid URL param (e.g. ?status=bogus) should
    # silently drop that filter, not blank the whole observation list.
    strict = False

    def get_breadcrumbs(self):
        return [{"label": _("Memory"), "url": reverse("memory:list")}, {"label": self.kwargs["repo_id"], "url": None}]

    def get_queryset(self):
        return (
            MemoryObservation.objects
            .filter(repo_id=self.kwargs["repo_id"])
            .select_related("run")
            .order_by("-created_at")
        )

    def get(self, request, *args, **kwargs):
        repo_id = self.kwargs["repo_id"]
        self.memory = RepositoryMemory.objects.filter(repo_id=repo_id).first()
        # Unfiltered repo total: drives the 404 guard and the "N observations so far" copy,
        # independent of any active status/category filter.
        self.total_observations = self.get_queryset().count()
        if self.memory is None and self.total_observations == 0:
            raise Http404("no memory for repository")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        cleaned = ctx["filter"].form.cleaned_data if ctx["filter"].form.is_valid() else {}
        memory = self.memory
        ctx.update({
            "repo_id": self.kwargs["repo_id"],
            "memory": memory,
            "total_observations": self.total_observations,
            "current_status": cleaned.get("status") or "",
            "current_category": cleaned.get("category") or "",
            "statuses": ObservationStatus.choices,
            "categories": ObservationCategory.choices,
            "document_lines": len(memory.content.splitlines()) if memory else 0,
            "document_bytes": len(memory.content.encode("utf-8")) if memory else 0,
            "memory_enabled": site_settings.memory_enabled,
        })
        return ctx


class MemoryConsolidateView(AdminRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, repo_id):
        if not site_settings.memory_enabled:
            messages.warning(request, _("Memory capture is disabled site-wide; consolidation was not queued."))
            return redirect("memory:detail", repo_id=repo_id)

        # Mirror the task's own guard so we don't report success for a run it will silently skip:
        # ``consolidate_memory_task`` no-ops when the repo has no pending observations.
        pending = MemoryObservation.objects.filter(repo_id=repo_id, status=ObservationStatus.PENDING).count()
        if pending == 0:
            messages.info(
                request, _("Nothing to consolidate for %(repo)s — no pending observations.") % {"repo": repo_id}
            )
        else:
            consolidate_memory_task.enqueue(repo_id)
            messages.success(
                request,
                _("Consolidation queued for %(repo)s (%(count)d pending observation(s)).")
                % {"repo": repo_id, "count": pending},
            )
        return redirect("memory:detail", repo_id=repo_id)
