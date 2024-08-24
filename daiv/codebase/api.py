from django.shortcuts import render

from ninja import Router

from codebase.clients import RepoClient

router = Router()


@router.get("/repositories/")
def search_repositories(request, search: str | None = None):
    repo_client = RepoClient.create_instance()
    repositories = [] if search is None else repo_client.list_repositories(search=search)
    return render(request, "codebase/search_repositories.html", {"repositories": repositories})
