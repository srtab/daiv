import abc
import logging
import tempfile
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import cast
from zipfile import ZipFile

from gitlab import Gitlab, GitlabCreateError

from .base import ClientType, FileChange, MergeRequestDiff, Repository
from .conf import settings

logger = logging.getLogger(__name__)


class RepoClient(abc.ABC):
    """
    Abstract class for repository clients.
    """

    client_slug: ClientType

    @abc.abstractmethod
    def get_repository(self, repo_id) -> Repository:
        """
        Get a repository.
        """
        pass

    @abc.abstractmethod
    def list_repositories(self, topics: list[str] | None = None) -> list[Repository]:
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
    def update_or_create_merge_request(
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
    def load_repo(self, repo_id: str, sha: str | None = None) -> AbstractContextManager[Path]:
        """
        Load a repository to a temporary directory.
        """
        pass

    @abc.abstractmethod
    def get_repo_head_sha(self, repo_id: str, branch: str | None = None) -> str:
        """
        Get the head sha of a repository.
        """
        pass

    @abc.abstractmethod
    def get_commit_changed_files(
        self, repo_id: str, from_sha: str, to_sha: str
    ) -> tuple[list[str], list[str], list[str]]:
        """
        Get the changed files between two commits.
        """

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

    client: Gitlab
    client_slug: ClientType = "gitlab"

    def __init__(self, auth_token: str, url: str | None = None):
        self.client = Gitlab(url=url, private_token=auth_token, timeout=10)

    def get_repository(self, repo_id: str) -> Repository:
        """
        Get a repository.
        """
        project = self.client.projects.get(repo_id)
        return Repository(
            pk=cast(str, project.get_id()),
            slug=project.path_with_namespace,
            default_branch=project.default_branch,
            client=self.client_slug,
            topics=project.topics,
            head_sha=self.get_repo_head_sha(cast(str, project.get_id()), branch=project.default_branch),
        )

    def list_repositories(self, topics: list[str] | None = None) -> list[Repository]:
        """
        List all repositories.
        """
        return [
            Repository(
                pk=cast(str, project.get_id()),
                slug=project.path_with_namespace,
                default_branch=project.default_branch,
                client=self.client_slug,
                topics=project.topics,
                head_sha=self.get_repo_head_sha(cast(str, project.get_id()), branch=project.default_branch),
            )
            for project in self.client.projects.list(
                all=True, iterator=True, archived=False, topic=topics and ",".join(topics), simple=True
            )
        ]

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

    def update_or_create_merge_request(
        self, repo_id: str, source_branch: str, target_branch: str, title: str, description: str
    ) -> int | str | None:
        """
        Create a merge request in a repository or update an existing one if it already exists.
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
                merge_request = merge_requests.next()
                merge_request.title = title
                merge_request.description = description
                merge_request.save()
                return merge_request.get_id()
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
                action["content"] = cast(str, file_change.content)
            if file_change.action == "move":
                action["previous_path"] = cast(str, file_change.previous_path)
            actions.append(action)

        project.commits.create({
            "branch": target_branch,
            "start_branch": ref,
            "commit_message": commit_message,
            "actions": actions,
            "force": True,
        })

    @contextmanager
    def load_repo(self, repo_id: str, sha: str | None = None) -> AbstractContextManager[Path]:
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
            raise
        else:
            yield Path(tmpdir.name).joinpath(repo_dirname)
        finally:
            tmpdir.cleanup()

    def get_repo_head_sha(self, repo_id: str, branch: str | None = None) -> str:
        """
        Get the head sha of a repository.
        """
        project = self.client.projects.get(repo_id, lazy=branch is not None)
        branch = branch or project.default_branch
        return project.branches.get(branch).commit["id"]

    def get_commit_changed_files(
        self, repo_id: str, from_sha: str, to_sha: str
    ) -> tuple[list[str], list[str], list[str]]:
        """
        Get the changed files between two commits.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        new_files = []
        changed_files = []
        deleted_files = []
        for diff in project.repository_compare(from_sha, to_sha)["diffs"]:
            if diff["new_file"]:
                new_files.append(diff["new_path"])
            elif diff["deleted_file"]:
                deleted_files.append(diff["old_path"])
            else:
                changed_files.append(diff["new_path"])
        return new_files, changed_files, deleted_files


class GitHubClient(RepoClient):
    """
    GitHub client to interact with GitHub repositories.
    """
