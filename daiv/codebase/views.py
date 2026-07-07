"""HTMX-fragment views for the prompt-box pickers.

These views return HTML fragments intended to be swapped into an existing
Alpine + HTMX scope. They are not JSON endpoints and are not part of the
Ninja API under ``/api/``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render

from github import GithubException
from gitlab.exceptions import GitlabError
from requests.exceptions import RequestException

from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, can_view, search_viewable_repositories
from codebase.clients import RepoClient

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger("daiv.codebase")

# Transient platform/network failures render as a "Could not load" row. Auth/config
# errors (ImproperlyConfigured, missing tokens, Django's SuspiciousOperation, bugs)
# propagate so operators see them instead of a silent UI fallback.
_PICKER_CLIENT_ERRORS: tuple[type[Exception], ...] = (GitlabError, GithubException, RequestException)

PICKER_LIMIT = 10


@login_required
def picker_repositories_view(request: HttpRequest) -> HttpResponse:
    """HTMX fragment: up to ``PICKER_LIMIT`` viewable repositories matching ``?q=``.

    Served from the local ``RepositoryCatalog`` mirror (a DB query), so there is no live platform
    call to fail here — a DB error is a real 500, not a "could not load" row.
    """
    query = request.GET.get("q", "").strip()
    repos = search_viewable_repositories(request.user, search=query or None, limit=PICKER_LIMIT)
    return render(request, "codebase/_repo_picker_list.html", {"repos": repos})


@login_required
def picker_branches_view(request: HttpRequest, slug: str) -> HttpResponse:
    """HTMX fragment: up to ``PICKER_LIMIT`` branches for ``slug`` filtered by ``?q=``. ``?selected=`` gets a ✓."""
    if not can_view(request.user, slug):
        raise Http404(REPO_ACCESS_DENIED_MESSAGE)
    query = request.GET.get("q", "").strip()
    selected = request.GET.get("selected", "")
    client = RepoClient.create_instance()
    try:
        branches = client.list_branches(slug, search=query or None, limit=PICKER_LIMIT)
    except _PICKER_CLIENT_ERRORS:
        logger.exception("picker_branches_view failed slug=%s q=%r user=%s", slug, query, request.user.pk)
        return render(request, "codebase/_branch_picker_list.html", {"error": True})
    return render(request, "codebase/_branch_picker_list.html", {"branches": branches, "selected": selected})
