from celery import group

from aequatioai.celery import app

from .clients import RepoClient
from .indexers import CodebaseIndex


@app.task
def update_index_by_repo_id(repo_ids: list[str], reset: bool = False):
    """
    Update the index of all repositories with the given IDs.
    """
    tasks = group([update_index_repository.s(repo_id, reset) for repo_id in repo_ids])
    tasks.apply_async()


@app.task
def update_index_by_topics(topics: list[str], reset: bool = False):
    """
    Update the index of all repositories with the given topics.
    """
    repo_client = RepoClient.create_instance()
    tasks = group([
        update_index_repository.s(repo_id, reset) for repo_id in repo_client.list_repositories(topics=topics)
    ])
    tasks.apply_async()


@app.task
def update_index_repository(repo_id: str, reset: bool = False):
    """
    Update codebase index of a repository.
    """
    repo_client = RepoClient.create_instance()
    indexer = CodebaseIndex(repo_client=repo_client)
    if reset:
        indexer.reset(repo_id=repo_id)
    indexer.update(repo_id=repo_id)
