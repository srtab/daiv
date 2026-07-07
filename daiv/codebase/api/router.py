from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django Ninja

from ninja import Router
from ninja.security import django_auth

from codebase.api.schemas import RepositorySearchResult
from codebase.authorization import search_viewable_repositories

router = Router(tags=["codebase"])

_SEARCH_LIMIT = 10


@router.get(
    "/repositories/search", response=list[RepositorySearchResult], auth=django_auth, url_name="search_repositories"
)
def search_repositories(request: HttpRequest, q: str = "") -> list[RepositorySearchResult]:
    """Search repositories by name for autocomplete (served from the local catalog mirror)."""
    if len(q) < 2:
        return []

    repos = search_viewable_repositories(request.user, search=q, limit=_SEARCH_LIMIT)
    return [RepositorySearchResult(slug=repo.slug, name=repo.name) for repo in repos]
