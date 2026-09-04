"""Server-rendered views for the codebase app.

The picker views return HTML fragments intended to be swapped into an existing Alpine + HTMX
scope; they are not JSON endpoints and are not part of the Ninja API under ``/api/``. The
cross-project access log is a full page.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render

from django_filters.views import FilterView
from github import GithubException
from gitlab.exceptions import GitlabError
from requests.exceptions import RequestException

from accounts.mixins import AdminRequiredMixin
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, can_view, search_viewable_repositories
from codebase.clients import RepoClient
from codebase.filters import CrossProjectAccessRecordFilterSet
from codebase.models import CrossProjectAccessRecord

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


class CrossProjectAccessLogView(AdminRequiredMixin, FilterView):
    """Answers SC-007: which projects were reached in a run, and under whose identity.

    Admin-only — the log names people and the projects they reached, which is more than a member
    is entitled to see about their colleagues.
    """

    model = CrossProjectAccessRecord
    filterset_class = CrossProjectAccessRecordFilterSet
    template_name = "codebase/cross_project_access.html"
    context_object_name = "records"
    ordering = ["-occurred_at"]
    paginate_by = 50
    # Invalid URL params (e.g. ?outcome=bogus) drop silently instead of blanking the list.
    strict = False

    def get_queryset(self):
        return super().get_queryset().select_related("acting_user")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context["filter"].form
        cleaned = form.cleaned_data if form.is_valid() else {}
        context["search_query"] = cleaned.get("target_repo_id") or ""
        context["current_outcome"] = cleaned.get("outcome") or ""
        context["thread_query"] = cleaned.get("thread_id") or ""
        context["outcome_choices"] = CrossProjectAccessRecord.Outcome.choices
        return context
