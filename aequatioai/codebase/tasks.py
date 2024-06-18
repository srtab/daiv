from celery import shared_task

from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex


@shared_task
def update_index_by_repo_id(repo_ids: list[str], reset: bool = False):
    """
    Update the index of all repositories with the given IDs.
    """
    for repo_id in repo_ids:
        update_index_repository(repo_id, reset)


@shared_task
def update_index_by_topics(topics: list[str], reset: bool = False):
    """
    Update the index of all repositories with the given topics.
    """
    repo_client = RepoClient.create_instance()
    for project in repo_client.list_repositories(topics=topics, load_all=True):
        update_index_repository(project.slug, reset)


@shared_task
def update_index_repository(repo_id: str, reset: bool = False):
    """
    Update codebase index of a repository.
    """
    repo_client = RepoClient.create_instance()
    indexer = CodebaseIndex(repo_client=repo_client)
    if reset:
        indexer.reset(repo_id=repo_id)
    indexer.update(repo_id=repo_id)
