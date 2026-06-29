from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.views.generic import TemplateView

from core.site_settings import site_settings
from memory.models import MemoryObservation, ObservationStatus, RepositoryMemory


class MemoryListView(LoginRequiredMixin, TemplateView):
    template_name = "memory/list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        obs_rows = {
            row["repo_id"]: row
            for row in MemoryObservation.objects.values("repo_id").annotate(
                total=Count("pk"),
                pending=Count("pk", filter=Q(status=ObservationStatus.PENDING)),
                consolidated=Count("pk", filter=Q(status=ObservationStatus.CONSOLIDATED)),
                discarded=Count("pk", filter=Q(status=ObservationStatus.DISCARDED)),
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
                "consolidated": obs["consolidated"] if obs else 0,
                "discarded": obs["discarded"] if obs else 0,
                "has_document": bool(mem and mem.content.strip()),
                "last_consolidated_at": mem.last_consolidated_at if mem else None,
            })

        ctx["repos"] = repos
        ctx["memory_enabled"] = site_settings.memory_enabled
        return ctx
