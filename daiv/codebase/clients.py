from __future__ import annotations

import abc
import logging
import tempfile
from contextlib import AbstractContextManager, contextmanager
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from zipfile import ZipFile

from gitlab import Gitlab, GitlabCreateError, GitlabHeadError, GitlabHttpError
from gitlab.v4.objects import ProjectHook

from .base import (
    ClientType,
    Discussion,
    FileChange,
    Issue,
    MergeRequest,
    MergeRequestDiff,
    Note,
    NoteDiffPosition,
    NotePosition,
    NotePositionLineRange,
    NoteType,
    Repository,
    User,
)
from .conf import settings

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)


class RepoClient(abc.ABC):
    """
    Abstract class for repository clients.
    """

    client_slug: ClientType

    @abc.abstractmethod
    def get_repository(self, repo_id) -> Repository:
        pass

    @abc.abstractmethod
    def list_repositories(
        self, search: str | None = None, topics: list[str] | None = None, load_all: bool = False
    ) -> list[Repository]:
        pass

    @abc.abstractmethod
    def get_repository_file(self, repo_id: str, file_path: str, ref: str | None = None) -> str | None:
        pass

    @abc.abstractmethod
    def repository_file_exists(self, repo_id: str, file_path: str, ref: str | None = None) -> bool:
        pass

    @abc.abstractmethod
    def get_repository_tree(
        self,
        repo_id: str,
        ref: str | None = None,
        *,
        path: str = "",
        recursive: bool = False,
        tree_type: Literal["blob", "tree"] | None = None,
    ) -> list[str]:
        pass

    @abc.abstractmethod
    def get_merge_request_diff(self, repo_id: str, merge_request_id: int) -> Generator[MergeRequestDiff, None, None]:
        pass

    @abc.abstractmethod
    def update_or_create_merge_request(
        self,
        repo_id: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        labels: list[str] | None = None,
    ) -> int | str | None:
        pass

    @abc.abstractmethod
    def comment_merge_request(self, repo_id: str, merge_request_id: int, body: str):
        pass

    @abc.abstractmethod
    def commit_changes(
        self,
        repo_id: str,
        target_branch: str,
        commit_message: str,
        file_changes: list[FileChange],
        start_branch: str | None = None,
        override_commits: bool = False,
    ):
        pass

    @abc.abstractmethod
    def load_repo(self, repo_id: str, sha: str | None = None) -> AbstractContextManager[Path]:
        pass

    @abc.abstractmethod
    def get_repo_head_sha(self, repo_id: str, branch: str | None = None) -> str:
        pass

    @abc.abstractmethod
    def get_commit_changed_files(
        self, repo_id: str, from_sha: str, to_sha: str
    ) -> tuple[list[str], list[str], list[str]]:
        pass

    @abc.abstractmethod
    def get_issue(self, repo_id: str, issue_id: int) -> Issue:
        pass

    @abc.abstractmethod
    def comment_issue(self, repo_id: str, issue_id: int, body: str):
        pass

    @abc.abstractmethod
    def get_issue_notes(self, repo_id: str, issue_id: int) -> list[Note]:
        pass

    @abc.abstractmethod
    def get_issue_related_merge_requests(
        self, repo_id: str, issue_id: int, assignee_id: int | None = None, label: str | None = None
    ) -> list[MergeRequest]:
        pass

    @abc.abstractmethod
    @cached_property
    def current_user(self) -> User:
        pass

    @abc.abstractmethod
    def get_merge_request_discussions(
        self, repo_id: str, merge_request_id: int, note_type: NoteType | None = None
    ) -> list[Discussion]:  # noqa: A002
        pass

    @abc.abstractmethod
    def resolve_merge_request_discussion(self, repo_id: str, merge_request_id: int, discussion_id: str):
        pass

    @abc.abstractmethod
    def create_merge_request_discussion_note(self, repo_id: str, merge_request_id: int, discussion_id: str, body: str):
        pass

    @staticmethod
    def create_instance() -> AllRepoClient:
        """
        Get the repository client based on the configuration.

        Returns:
            The repository client instance.
        """
        if settings.CODEBASE_CLIENT == ClientType.GITLAB:
            return GitLabClient(auth_token=settings.CODEBASE_GITLAB_AUTH_TOKEN, url=settings.CODEBASE_GITLAB_URL)
        if settings.CODEBASE_CLIENT == ClientType.GITHUB:
            raise NotImplementedError("GitHub client is not implemented yet")
        raise ValueError("Invalid repository client configuration")


class GitLabClient(RepoClient):
    """
    GitLab client to interact with GitLab repositories.
    """

    client: Gitlab
    client_slug = ClientType.GITLAB

    def __init__(self, auth_token: str, url: str | None = None):
        self.client = Gitlab(url=url, private_token=auth_token, timeout=10, keep_base_url=True)

    def get_repository(self, repo_id: str) -> Repository:
        """
        Get a repository.

        Args:
            repo_id: The repository ID.

        Returns:
            The repository object.
        """
        project = self.client.projects.get(repo_id)
        return Repository(
            pk=cast(int, project.get_id()),
            slug=project.path_with_namespace,
            name=project.name,
            default_branch=project.default_branch,
            client=self.client_slug,
            topics=project.topics,
            head_sha=self.get_repo_head_sha(repo_id, branch=project.default_branch),
        )

    def list_repositories(
        self, search: str | None = None, topics: list[str] | None = None, load_all: bool = False
    ) -> list[Repository]:
        """
        List all repositories.

        Args:
            search: The search query.
            topics: The topics to filter the repositories.
            load_all: Load all repositories.

        Returns:
            The list of repositories.
        """
        optional_kwargs = {}
        if search:
            optional_kwargs["search"] = search
        if topics:
            optional_kwargs["topic"] = ",".join(topics)
        return [
            Repository(
                pk=cast(int, project.get_id()),
                slug=project.path_with_namespace,
                name=project.name,
                default_branch=project.default_branch,
                client=self.client_slug,
                topics=project.topics,
                head_sha=self.get_repo_head_sha(cast(int, project.get_id()), branch=project.default_branch),
            )
            for project in self.client.projects.list(
                all=load_all, iterator=True, archived=False, simple=True, **optional_kwargs
            )
        ]

    def get_repository_file(self, repo_id: str, file_path: str, ref: str | None = None) -> str | None:
        """
        Get the content of a file in a repository.

        Args:
            repo_id: The repository ID.
            file_path: The file path.
            ref: The branch or tag name.

        Returns:
            The content of the file. If the file is binary or not a text file, it returns None.
        """
        project = self.client.projects.get(repo_id)
        try:
            project_file = project.files.get(file_path=file_path, ref=ref or project.default_branch)
        except GitlabHttpError as e:
            if e.response_code == 404:
                return None
            raise e
        try:
            return project_file.decode().decode()
        except UnicodeDecodeError:
            return None

    def repository_file_exists(self, repo_id: str, file_path: str, ref: str | None = None) -> bool:
        """
        Check if a file exists in a repository.

        Args:
            repo_id: The repository ID.
            file_path: The file path.
            ref: The branch or tag name.

        Returns:
            True if the file exists, otherwise False.
        """
        project = self.client.projects.get(repo_id)
        try:
            project.files.head(file_path=file_path, ref=ref or project.default_branch)
        except GitlabHeadError as e:
            if e.response_code == 404:
                return False
            raise e
        return True

    def get_repository_tree(
        self,
        repo_id: str,
        ref: str | None = None,
        *,
        path: str = "",
        recursive: bool = False,
        tree_type: Literal["blob", "tree"] | None = None,
    ) -> list[str]:
        """
        Get the tree of a repository.

        Args:
            repo_id: The repository ID.
            ref: The branch or tag name.
            path: The path to list the tree.
            recursive: Recursively list the tree.
            tree_type: The type of the tree to filter. If None, it returns all types.

        Returns:
            The list of files or directories in the tree.
        """
        project = self.client.projects.get(repo_id)
        repository_tree = project.repository_tree(
            recursive=recursive, ref=ref or project.default_branch, path=path, all=True
        )
        return [file["path"] for file in repository_tree if tree_type is None or file["type"] == tree_type]

    def set_repository_webhooks(
        self,
        repo_id: str,
        url: str,
        events: list[
            Literal["push_events", "merge_requests_events", "issues_events", "pipeline_events", "note_events"]
        ],
        push_events_branch_filter: str | None = None,
        enable_ssl_verification: bool = True,
    ):
        """
        Set webhooks for a repository.
        If the webhook already exists, it updates the existing one. Otherwise, it creates a new one.

        Args:
            repo_id: The repository ID.
            url: The webhook URL.
            events: The list of events to trigger the webhook.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        data = {
            "url": url,
            "name": "DAIV",
            "description": "WebHooks for DAIV integration.",
            "push_events": "push_events" in events,
            "merge_requests_events": "merge_requests_events" in events,
            "issues_events": "issues_events" in events,
            "pipeline_events": "pipeline_events" in events,
            "note_events": "note_events" in events,
            "enable_ssl_verification": enable_ssl_verification,
        }
        if push_events_branch_filter:
            data["push_events_branch_filter"] = push_events_branch_filter
        if project_hook := self._get_repository_hook_by_name(repo_id, data["name"]):
            for key, value in data.items():
                setattr(project_hook, key, value)
            project_hook.save()
        else:
            project.hooks.create(data)

    def _get_repository_hook_by_name(self, repo_id: str, name: str) -> ProjectHook | None:
        """
        Get a webhook by name.

        Args:
            repo_id: The repository ID.
            name: The webhook name.

        Returns:
            The webhook object if it exists, otherwise None.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        for hook in project.hooks.list(all=True, iterator=True):
            if hook.name == name:
                return cast(ProjectHook, hook)
        return None

    def get_merge_request_diff(self, repo_id: str, merge_request_id: int) -> Generator[MergeRequestDiff, None, None]:
        """
        Get the latest diff of a merge request.
        https://docs.gitlab.com/ee/administration/instance_limits.html#diff-limits

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.

        Returns:
            The generator of merge request diffs.
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
        self,
        repo_id: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        labels: list[str] | None = None,
    ) -> int | str | None:
        """
        Create a merge request in a repository or update an existing one if it already exists.

        Args:
            repo_id: The repository ID.
            source_branch: The source branch.
            target_branch: The target branch.
            title: The title of the merge request.
            description: The description of the merge request.
            labels: The list of labels.

        Returns:
            The merge request ID.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        try:
            return project.mergerequests.create({
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
                "labels": labels or [],
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
                merge_request.labels = labels or []
                merge_request.save()
                return merge_request.get_id()
            raise e

    def comment_merge_request(self, repo_id: str, merge_request_id: int, body: str):
        """
        Comment on a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            body: The comment body.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        merge_request.notes.create({"body": body})

    def commit_changes(
        self,
        repo_id: str,
        target_branch: str,
        commit_message: str,
        file_changes: list[FileChange],
        start_branch: str | None = None,
        override_commits: bool = False,
    ):
        """
        Commit changes to a repository.

        Args:
            repo_id: The repository ID.
            ref: The branch or tag name.
            target_branch: The target branch.
            commit_message: The commit message.
            file_changes: The list of file changes.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        actions: list[dict[str, str]] = []

        for file_change in file_changes:
            action = {"action": file_change.action, "file_path": file_change.file_path}
            if file_change.action in ["create", "update"]:
                action["content"] = cast(str, file_change.content)
            if file_change.action == "move":
                action["previous_path"] = cast(str, file_change.previous_path)
                # Move actions that do not specify content preserve the existing file content,
                # and any other value of content overwrites the file content.
                if file_change.content:
                    action["content"] = cast(str, file_change.content)
            actions.append(action)

        commits = {
            "branch": target_branch,
            "commit_message": commit_message,
            "actions": actions,
            "force": override_commits,
        }

        if start_branch:
            commits["start_branch"] = start_branch

        project.commits.create(commits)

    @contextmanager
    def load_repo(self, repo_id: str, sha: str | None = None) -> AbstractContextManager[Path]:
        """
        Load a repository to a temporary directory.

        Args:
            repo_id: The repository ID.
            sha: The commit sha.

        Yields:
            The path to the repository directory.
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

    def get_repo_head_sha(self, repo_id: str | int, branch: str | None = None) -> str:
        """
        Get the head sha of a repository.

        Args:
            repo_id: The repository ID.
            branch: The branch name.

        Returns:
            The head sha of the repository.
        """
        project = self.client.projects.get(repo_id, lazy=branch is not None)
        branch = branch or project.default_branch
        return project.branches.get(branch).commit["id"]

    def get_commit_changed_files(
        self, repo_id: str, from_sha: str, to_sha: str
    ) -> tuple[list[str], list[str], list[str]]:
        """
        Get the changed files between two commits.

        Args:
            repo_id: The repository ID.
            from_sha: The from commit sha.
            to_sha: The to commit sha.

        Returns:
            The tuple of new files, changed files, and deleted files.
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

    def get_issue(self, repo_id: str, issue_id: int) -> Issue:
        """
        Get an issue.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.

        Returns:
            The issue object.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id)
        return Issue(
            id=issue.iid,
            title=issue.title,
            description=issue.description,
            state=issue.state,
            notes=self.get_issue_notes(repo_id, issue_id),
            related_merge_requests=self.get_issue_related_merge_requests(repo_id, issue_id),
        )

    def comment_issue(self, repo_id: str, issue_id: int, body: str):
        """
        Comment on an issue.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            body: The comment body.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id, lazy=True)
        issue.notes.create({"body": body})

    def get_issue_notes(self, repo_id: str, issue_id: int) -> list[Note]:
        """
        Get the notes of an issue.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.

        Returns:
            The list of issue notes.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id, lazy=True)
        return [
            Note(
                id=note.id,
                body=note.body,
                type=note.type,
                noteable_type=note.noteable_type,
                system=note.system,
                resolvable=note.resolvable,
                resolved=note.resolvable and note.resolved or None,
                author=User(
                    id=note.author.get("id"), username=note.author.get("username"), name=note.author.get("name")
                ),
            )
            for note in issue.notes.list(all=True)
            if not note.system and not note.resolvable
        ]

    def get_issue_related_merge_requests(
        self, repo_id: str, issue_id: int, assignee_id: int | None = None, label: str | None = None
    ) -> list[MergeRequest]:
        """
        Get the related merge requests of an issue.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            assignee_id: The assignee ID.

        Returns:
            The list of merge requests.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id, lazy=True)
        return [
            MergeRequest(repo_id=repo_id, merge_request_id=cast(int, mr["iid"]), source_branch=mr["source_branch"])
            for mr in issue.related_merge_requests(all=True)
            if (assignee_id is None or mr["assignee"] and mr["assignee"]["id"] == assignee_id)
            and (label is None or label in mr["labels"])
        ]

    @cached_property
    def current_user(self) -> User:
        """
        Get the profile of the current user.

        Returns:
            The profile of the user.
        """
        self.client.auth()
        if user := self.client.user:
            return User(id=user.id, username=user.username, name=user.name)
        raise ValueError("Couldn't get current user profile")

    def get_merge_request_discussions(
        self, repo_id: str, merge_request_id: int, note_type: NoteType | None = None
    ) -> list[Discussion]:  # noqa: A002
        """
        Get the discussions from a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            note_type: The note type.

        Returns:
            The list of discussions.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        return [
            Discussion(id=discussion.id, notes=notes)
            for discussion in merge_request.discussions.list(all=True, iterator=True)
            if discussion.individual_note is False
            and (notes := self._serialize_notes(discussion.attributes["notes"], note_type))
        ]

    def _serialize_notes(self, notes: list[dict], note_type: NoteType | None = None) -> list[Note]:
        """
        Serialize dictionary of notes to Note objects.

        Args:
            notes: The list of notes.
            note_type: The note type.

        Returns:
            The list of Note objects.
        """
        return [
            Note(
                id=note["id"],
                body=note["body"],
                type=note["type"],
                noteable_type=note["noteable_type"],
                system=note["system"],
                resolvable=note["resolvable"],
                resolved=note["resolved"],
                author=User(
                    id=note["author"].get("id"),
                    username=note["author"].get("username"),
                    name=note["author"].get("name"),
                ),
                position=NotePosition(
                    head_sha=note["position"].get("head_sha"),
                    old_path=note["position"].get("old_path"),
                    new_path=note["position"].get("new_path"),
                    position_type=note["position"].get("position_type"),
                    old_line=note["position"].get("old_line"),
                    new_line=note["position"].get("new_line"),
                    line_range=NotePositionLineRange(
                        start=NoteDiffPosition(
                            type=note["position"]["line_range"]["start"]["type"],
                            old_line=note["position"]["line_range"]["start"]["old_line"],
                            new_line=note["position"]["line_range"]["start"]["new_line"],
                        ),
                        end=NoteDiffPosition(
                            type=note["position"]["line_range"]["end"]["type"],
                            old_line=note["position"]["line_range"]["end"]["old_line"],
                            new_line=note["position"]["line_range"]["end"]["new_line"],
                        ),
                    )
                    if note["position"].get("line_range")
                    else None,
                ),
            )
            for note in notes
            if not note["system"]
            and note["resolvable"]
            and not note["resolved"]
            and (note_type is None or note["type"] == note_type)
        ]

    def resolve_merge_request_discussion(self, repo_id: str, merge_request_id: int, discussion_id: str):
        """
        Resolve a discussion in a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            discussion_id: The discussion ID.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        merge_request.discussions.update(discussion_id, {"resolved": True})

    def create_merge_request_discussion_note(self, repo_id: str, merge_request_id: int, discussion_id: str, body: str):
        """
        Create a note in a discussion of a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            discussion_id: The discussion ID.
            body: The note body.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        discussion = merge_request.discussions.get(discussion_id, lazy=True)
        discussion.notes.create({"body": body})


class GitHubClient(RepoClient):
    """
    GitHub client to interact with GitHub repositories.

    Note: This class is not implemented yet. It is a placeholder for future development.
    """

    client_slug = ClientType.GITHUB


AllRepoClient = GitHubClient | GitLabClient
