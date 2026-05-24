"""Authenticated JSON views for the automation app."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from asgiref.sync import async_to_sync

from automation.agent.model_catalog.service import fetch_catalog
from core.models import Provider


@require_GET
@login_required
def agent_models_view(request: HttpRequest) -> HttpResponse:
    """Return the model catalog for the agent picker.

    Response shape::

        {
          "providers": [{"slug": "...", "label": "..."}, ...],
          "catalog": {
            "<slug>": {"models": [...], "error": null | "..."},
            ...
          }
        }

    Disabled providers are filtered out. HTTP 200 even when individual
    providers errored — the per-provider error lives in the payload.
    """
    enabled_rows = [row for row in Provider.get_cached_rows() if row.is_enabled]
    catalog = async_to_sync(fetch_catalog)(enabled_rows)

    return JsonResponse({
        "providers": [
            {"slug": row.slug, "label": row.display_name or row.slug.replace("_", " ").title()} for row in enabled_rows
        ],
        "catalog": {slug: {"models": entry.models, "error": entry.error} for slug, entry in catalog.items()},
    })
