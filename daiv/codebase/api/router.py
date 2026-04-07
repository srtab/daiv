from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django Ninja

from ninja import Router
from ninja.security import django_auth

from codebase.api.schemas import RepositorySearchResult
from codebase.clients import RepoClient

router = Router(tags=["codebase"])


@router.get("/repositories/search", response=list[RepositorySearchResult], auth=django_auth)
def search_repositories(request: HttpRequest, q: str = "") -> list[RepositorySearchResult]:
    """Search repositories by name for autocomplete."""
    if len(q) < 2:
        return []

    client = RepoClient.create_instance()
    repos = client.list_repositories(search=q, limit=10)
    return [RepositorySearchResult(slug=repo.slug, name=repo.name) for repo in repos]
