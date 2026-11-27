"""HTMX-fragment views for the prompt-box pickers.

These views return HTML fragments intended to be swapped into an existing
Alpine + HTMX scope. They are not JSON endpoints and are not part of the
Ninja API under ``/api/``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import urlencode

from codebase.authorization import (
    REPO_ACCESS_DENIED_MESSAGE,
    can_view,
    runnable_repository_facets,
    runnable_repository_matches,
    search_runnable_repositories,
)
from codebase.base import MAX_REPOS_PER_BATCH
from codebase.clients import RepoClient
from codebase.clients.base import PLATFORM_ERRORS

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger("daiv.codebase")

PICKER_LIMIT = 10


def _selected_slugs(raw: str) -> list[str]:
    return [slug for slug in (part.strip() for part in raw.split(",")) if slug][:MAX_REPOS_PER_BATCH]


@login_required
def picker_repositories_view(request: HttpRequest) -> HttpResponse:
    """HTMX fragment: one ``PICKER_LIMIT``-sized page of runnable repositories matching ``?q=``, ``?language=``
    and ``?topic=``.

    Served from the local ``RepositoryCatalog`` mirror (a DB query), so there is no live platform
    call to fail here — a DB error is a real 500, not a "could not load" row. Only repositories the
    user can run on are listed: every picker submits to a run target that requires WRITE.

    ``?after=<slug>`` requests the page after that slug and renders the rows *without* the ``<ul>``
    wrapper, so the popover's infinite-scroll sentinel can replace itself with them — plus the
    next sentinel, which is what keeps paging going past page 2 (see ``_repo_picker_options.html``).
    One extra row is fetched to decide whether another page exists.

    The first page of a filtered or searched list also carries the select-all summary in
    ``data-select-all``; ``?selected=`` (comma-separated slugs) tells it which selections match.
    """
    query = request.GET.get("q", "").strip()
    after = request.GET.get("after", "").strip()
    language = request.GET.get("language", "").strip()
    topic = request.GET.get("topic", "").strip()
    topics = [topic] if topic else None
    rows = search_runnable_repositories(
        request.user, search=query, topics=topics, language=language, limit=PICKER_LIMIT + 1, after_slug=after
    )
    repos = rows[:PICKER_LIMIT]
    next_url = None
    if len(rows) > PICKER_LIMIT:
        params = {"q": query, "language": language, "topic": topic, "after": repos[-1].slug}
        next_url = "{}?{}".format(reverse("codebase:picker-repositories"), urlencode(params))
    select_all = None
    if not after and (query or language or topic):
        matches = runnable_repository_matches(
            request.user,
            search=query,
            topics=topics,
            language=language,
            selected=_selected_slugs(request.GET.get("selected", "")),
            candidate_limit=MAX_REPOS_PER_BATCH,
        )
        select_all = json.dumps({
            "total": matches.total,
            "candidates": [{"slug": slug, "default_branch": branch} for slug, branch in matches.candidates],
            "matching_selected": matches.matching_selected,
        })
    template = "codebase/_repo_picker_options.html" if after else "codebase/_repo_picker_list.html"
    context = {
        "rows": [(repo, repo.language_label(language)) for repo in repos],
        "next_url": next_url,
        "select_all": select_all,
    }
    return render(request, template, context)


@login_required
def picker_repository_facets_view(request: HttpRequest) -> HttpResponse:
    """HTMX fragment: the picker's Language and Topic dropdowns, each option with how many repositories it keeps."""
    language = request.GET.get("language", "").strip()
    topic = request.GET.get("topic", "").strip()
    facets = runnable_repository_facets(request.user, topics=[topic] if topic else None, language=language)
    return render(
        request, "codebase/_repo_picker_filters.html", {"facets": facets, "language": language, "topic": topic}
    )


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
    except PLATFORM_ERRORS:
        logger.exception("picker_branches_view failed slug=%s q=%r user=%s", slug, query, request.user.pk)
        return render(request, "codebase/_branch_picker_list.html", {"error": True})
    return render(request, "codebase/_branch_picker_list.html", {"branches": branches, "selected": selected})
