"""HTMX-fragment views for the prompt-box pickers.

These views return HTML fragments intended to be swapped into an existing
Alpine + HTMX scope. They are not JSON endpoints and are not part of the
Ninja API under ``/api/``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from codebase.clients import RepoClient

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger("daiv.codebase")


@login_required
def picker_repositories_view(request: HttpRequest) -> HttpResponse:
    """HTMX fragment: up to 20 repositories matching ``?q=``."""
    query = request.GET.get("q", "").strip()
    client = RepoClient.create_instance()
    try:
        repos = client.list_repositories(search=query or None, limit=20)
    except Exception:
        logger.exception("picker_repositories_view: list_repositories failed")
        return render(request, "codebase/_repo_picker_list.html", {"error": True})
    return render(request, "codebase/_repo_picker_list.html", {"repos": repos})


@login_required
def picker_branches_view(request: HttpRequest, slug: str) -> HttpResponse:
    """HTMX fragment: up to 20 branches for ``slug``, filtered by ``?q=``. ``?selected=`` gets a ✓."""
    query = request.GET.get("q", "").strip()
    selected = request.GET.get("selected", "")
    client = RepoClient.create_instance()
    try:
        branches = client.list_branches(slug, search=query or None, limit=20)
    except Exception:
        logger.exception("picker_branches_view: list_branches failed for %s", slug)
        return render(request, "codebase/_branch_picker_list.html", {"error": True})
    return render(request, "codebase/_branch_picker_list.html", {"branches": branches, "selected": selected})
