import abc
from collections.abc import Generator

from gitlab import Gitlab

from aequatioai.codebase.models import MergeRequestDiff


class RepoClient(abc.ABC):
    """
    Abstract class for repository clients.
    """

    client: Gitlab

    @abc.abstractmethod
    def list_repositories(self, topics: list[str] | None = None) -> list:
        """
        List all repositories.
        """
        pass

    @abc.abstractmethod
    def get_repository_file(self, repo_id: str, file_path: str, ref: str | None = None) -> bytes:
        """
        Get the content of a file in a repository.
        """
        pass

    @abc.abstractmethod
    def get_repository_tree(self, repo_id: str, ref: str | None = None) -> list[str]:
        """
        Get the tree of a repository.
        """
        pass

    @abc.abstractmethod
    def get_merge_request_diff(self, repo_id: str, merge_request_id: str) -> Generator[MergeRequestDiff, None, None]:
        """
        Get the diff of a merge request.
        """
        pass


class GitLabClient(RepoClient):
    """
    GitLab client to interact with GitLab repositories.
    """

    def __init__(self, auth_token: str, url: str | None = None):
        self.client = Gitlab(url=url, private_token=auth_token, timeout=10)

    def list_repositories(self, topics: list[str] | None = None):
        """
        List all repositories.
        """
        return self.client.projects.list(all=True, iterator=True, topics=topics)

    def get_repository_file(self, repo_id: str, file_path: str, ref: str | None = None) -> bytes:
        """
        Get the content of a file in a repository.
        """
        project = self.client.projects.get(repo_id)
        project_file = project.files.get(file_path=file_path, ref=ref or project.default_branch)
        return project_file.decode()

    def get_repository_tree(self, repo_id: str, ref: str | None = None) -> list[str]:
        """
        Get the tree of a repository.
        """
        project = self.client.projects.get(repo_id)
        repository_tree = project.repository_tree(recursive=True, ref=ref or project.default_branch, all=True)
        return [file["path"] for file in repository_tree if file["type"] == "blob"]

    def get_merge_request_diff(self, repo_id: str, merge_request_id: str) -> Generator[MergeRequestDiff, None, None]:
        """
        Get the diff of a merge request.
        https://docs.gitlab.com/ee/administration/instance_limits.html#diff-limits
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        for mr_diff in merge_request.diffs.list(all=True):
            for diff in merge_request.diffs.get(mr_diff.id).diffs:
                yield MergeRequestDiff(
                    repo_id=repo_id,
                    merge_request_id=merge_request_id,
                    old_path=diff["old_path"],
                    new_path=diff["new_path"],
                    diff=diff["diff"],
                    new_file=diff["new_file"],
                    renamed_file=diff["renamed_file"],
                    deleted_file=diff["deleted_file"],
                )


class GitHubClient(RepoClient):
    pass
