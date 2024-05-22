import abc
import logging
import tempfile
from collections.abc import Generator
from pathlib import Path
from zipfile import ZipFile

from gitlab import Gitlab, GitlabCreateError

from .conf import settings
from .models import FileChange, MergeRequestDiff

logger = logging.getLogger(__name__)


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
    def get_repository_file(self, repo_id: str, file_path: str, ref: str | None = None) -> str | None:
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

    @abc.abstractmethod
    def get_or_create_merge_request(
        self, repo_id: str, source_branch: str, target_branch: str, title: str, description: str
    ) -> int | str | None:
        """
        Create a merge request in a repository or get an existing one if it already exists.
        """
        pass

    @abc.abstractmethod
    def commit_changes(
        self, repo_id: str, ref: str, target_branch: str, commit_message: str, file_changes: list[FileChange]
    ):
        """
        Commit changes to a repository.
        """
        pass

    @abc.abstractmethod
    def load_repo(self, repo_id: str, sha: str | None = None) -> tuple[Path, tempfile.TemporaryDirectory]:
        """
        Load a repository to a temporary directory.
        """
        pass

    @staticmethod
    def create_instance():
        """
        Get the repository client based on the configuration.
        """
        if settings.CODEBASE_CLIENT == "gitlab":
            return GitLabClient(auth_token=settings.CODEBASE_GITLAB_AUTH_TOKEN, url=settings.CODEBASE_GITLAB_URL)
        if settings.CODEBASE_CLIENT == "github":
            raise NotImplementedError("GitHub client is not implemented yet")
        raise ValueError("Invalid repository client configuration")


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

    def get_repository_file(self, repo_id: str, file_path: str, ref: str | None = None) -> str | None:
        """
        Get the content of a file in a repository.
        """
        project = self.client.projects.get(repo_id)
        project_file = project.files.get(file_path=file_path, ref=ref or project.default_branch)
        try:
            return project_file.decode().decode()
        except UnicodeDecodeError:
            return None

    def get_repository_tree(self, repo_id: str, ref: str | None = None) -> list[str]:
        """
        Get the tree of a repository.
        """
        project = self.client.projects.get(repo_id)
        repository_tree = project.repository_tree(recursive=True, ref=ref or project.default_branch, all=True)
        return [file["path"] for file in repository_tree if file["type"] == "blob"]

    def get_merge_request_diff(self, repo_id: str, merge_request_id: str) -> Generator[MergeRequestDiff, None, None]:
        """
        Get the latest diff of a merge request.
        https://docs.gitlab.com/ee/administration/instance_limits.html#diff-limits
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        # The first version is the one who has the latest changes, we don't need to get all history of the diffs.
        first_merge_request_version = merge_request.diffs.list(iterator=True).next()
        for version_diff in merge_request.diffs.get(first_merge_request_version.id, unidiff="true").diffs:
            if version_diff["generated_file"]:
                # ignore generated files, for more details:
                # https://docs.gitlab.com/ee/user/project/merge_requests/changes.html#collapse-generated-files
                continue
            yield MergeRequestDiff(
                repo_id=repo_id,
                merge_request_id=merge_request_id,
                ref=first_merge_request_version.head_commit_sha,
                old_path=version_diff["old_path"],
                new_path=version_diff["new_path"],
                diff=version_diff["diff"],
                new_file=version_diff["new_file"],
                renamed_file=version_diff["renamed_file"],
                deleted_file=version_diff["deleted_file"],
            )

    def get_or_create_merge_request(
        self, repo_id: str, source_branch: str, target_branch: str, title: str, description: str
    ) -> int | str | None:
        """
        Create a merge request in a repository or get an existing one if it already exists.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        try:
            return project.mergerequests.create({
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
            }).get_id()
        except GitlabCreateError as e:
            if e.response_code != 409:
                raise e
            if merge_requests := project.mergerequests.list(
                source_branch=source_branch, target_branch=target_branch, iterator=True
            ):
                return merge_requests.next().get_id()
            raise e

    def commit_changes(
        self, repo_id: str, ref: str, target_branch: str, commit_message: str, file_changes: list[FileChange]
    ):
        """
        Commit changes to a repository.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        actions: list[dict[str, str]] = []

        for file_change in file_changes:
            action = {"action": file_change.action, "file_path": file_change.file_path}
            if file_change.action in ["create", "update", "move"]:
                action["content"] = file_change.content
            if file_change.action == "move":
                action["previous_path"] = file_change.previous_path
            actions.append(action)

        return project.commits.create({
            "branch": target_branch,
            "start_branch": ref,
            "commit_message": commit_message,
            "actions": actions,
            "force": True,
        })

    def load_repo(self, repo_id: str, sha: str | None = None) -> tuple[Path, tempfile.TemporaryDirectory]:
        """
        Load a repository to a temporary directory.
        """
        project = self.client.projects.get(repo_id)
        sha = sha or project.default_branch

        tmpdir = tempfile.TemporaryDirectory(prefix=f"{project.get_id()}-{sha}-repo")
        logger.debug("Loading repository to %s", tmpdir)

        try:
            with tempfile.NamedTemporaryFile(prefix=f"{project.get_id()}-{sha}-archive", suffix=".zip") as repo_archive:
                project.repository_archive(streamed=True, action=repo_archive.write, format="zip", sha=sha)
                repo_archive.flush()
                repo_archive.seek(0)

                with ZipFile(repo_archive.name, "r") as zipfile:
                    zipfile.extractall(path=tmpdir.name)
                    # The first file in the archive is the repository directory.
                    repo_dirname = zipfile.filelist[0].filename
        except Exception:
            tmpdir.cleanup()
            raise

        return Path(tmpdir.name).joinpath(repo_dirname), tmpdir


class GitHubClient(RepoClient):
    """
    GitHub client to interact with GitHub repositories.
    """
