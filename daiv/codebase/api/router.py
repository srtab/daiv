from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django Ninja

from ninja import Router
from ninja.security import django_auth

from codebase.api.schemas import RepositorySearchResult
from codebase.authorization import filter_viewable, viewable_fetch_limit
from codebase.clients import RepoClient

router = Router(tags=["codebase"])

_SEARCH_LIMIT = 10


@router.get(
    "/repositories/search", response=list[RepositorySearchResult], auth=django_auth, url_name="search_repositories"
)
def search_repositories(request: HttpRequest, q: str = "") -> list[RepositorySearchResult]:
    """Search repositories by name for autocomplete."""
    if len(q) < 2:
        return []

    client = RepoClient.create_instance()
    repos = client.list_repositories(search=q, limit=viewable_fetch_limit(_SEARCH_LIMIT))
    repos = filter_viewable(request.user, repos)[:_SEARCH_LIMIT]
    return [RepositorySearchResult(slug=repo.slug, name=repo.name) for repo in repos]
