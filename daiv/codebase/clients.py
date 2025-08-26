from __future__ import annotations

import abc
import functools
import io
import logging
import tempfile
from contextlib import AbstractContextManager, contextmanager
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from zipfile import ZipFile

from gitlab import Gitlab, GitlabCreateError, GitlabGetError, GitlabOperationError

from core.constants import BOT_NAME
from core.utils import async_download_url, build_uri

from .base import (
    ClientType,
    Discussion,
    FileChange,
    Issue,
    Job,
    MergeRequest,
    MergeRequestDiff,
    Note,
    NoteDiffPosition,
    NotePosition,
    NotePositionLineRange,
    NoteType,
    Pipeline,
    Repository,
    User,
)
from .conf import settings

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from gitlab.v4.objects import ProjectHook

logger = logging.getLogger("daiv.clients")


class RepoClient(abc.ABC):
    """
    Abstract class for repository clients.
    """

    client_slug: ClientType

    @property
    @abc.abstractmethod
    def codebase_url(self) -> str:
        pass

    @abc.abstractmethod
    def get_repository(self, repo_id) -> Repository:
        pass

    @abc.abstractmethod
    def list_repositories(
        self, search: str | None = None, topics: list[str] | None = None, load_all: bool = False
    ) -> list[Repository]:
        pass

    @abc.abstractmethod
    def get_repository_file(self, repo_id: str, file_path: str, ref: str) -> str | None:
        pass

    @abc.abstractmethod
    def get_repository_file_link(self, repo_id: str, file_path: str, ref: str) -> str:
        pass

    @abc.abstractmethod
    def get_project_uploaded_file(self, repo_id: str, file_path: str) -> bytes | None:
        pass

    @abc.abstractmethod
    def repository_branch_exists(self, repo_id: str, branch: str) -> bool:
        pass

    @abc.abstractmethod
    def set_repository_webhooks(
        self,
        repo_id: str,
        url: str,
        events: list[Literal["push_events", "issues_events", "note_events", "pipeline_events"]],
        push_events_branch_filter: str | None = None,
        enable_ssl_verification: bool = True,
    ) -> bool:
        pass

    @abc.abstractmethod
    def get_merge_request_diff(self, repo_id: str, merge_request_id: int) -> Generator[MergeRequestDiff]:
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
        assignee_id: int | None = None,
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
    def load_repo(self, repo_id: str, sha: str) -> Iterator[Path]:
        pass

    @abc.abstractmethod
    def get_issue(self, repo_id: str, issue_id: int) -> Issue:
        pass

    @abc.abstractmethod
    def comment_issue(self, repo_id: str, issue_id: int, body: str):
        pass

    @abc.abstractmethod
    def create_issue_note_emoji(self, repo_id: str, issue_id: int, emoji: str, note_id: str):
        pass

    @abc.abstractmethod
    def get_issue_notes(self, repo_id: str, issue_id: int) -> list[Note]:
        pass

    @abc.abstractmethod
    def get_issue_discussions(self, repo_id: str, issue_id: int) -> list[Discussion]:
        pass

    @abc.abstractmethod
    def get_issue_discussion(self, repo_id: str, issue_id: int, discussion_id: str) -> Discussion:
        pass

    @abc.abstractmethod
    def get_issue_related_merge_requests(
        self, repo_id: str, issue_id: int, assignee_id: int | None = None, label: str | None = None
    ) -> list[MergeRequest]:
        pass

    @abc.abstractmethod
    def create_issue_discussion_note(self, repo_id: str, issue_id: int, body: str, discussion_id: str | None = None):
        pass

    @abc.abstractmethod
    @cached_property
    def current_user(self) -> User:
        pass

    @abc.abstractmethod
    def get_merge_request(self, repo_id: str, merge_request_id: int) -> MergeRequest:
        pass

    @abc.abstractmethod
    def get_merge_request_latest_pipeline(self, repo_id: str, merge_request_id: int) -> Pipeline | None:
        pass

    @abc.abstractmethod
    def get_merge_request_discussions(
        self, repo_id: str, merge_request_id: int, note_types: list[NoteType] | None = None
    ) -> list[Discussion]:  # noqa: A002
        pass

    @abc.abstractmethod
    def get_merge_request_discussion(self, repo_id: str, merge_request_id: int, discussion_id: str) -> Discussion:
        pass

    @abc.abstractmethod
    def update_merge_request_discussion_note(
        self, repo_id: str, merge_request_id: int, discussion_id: str, note_id: str, body: str
    ):
        pass

    @abc.abstractmethod
    def create_merge_request_note_emoji(self, repo_id: str, merge_request_id: int, emoji: str, note_id: str):
        pass

    @abc.abstractmethod
    def create_merge_request_discussion_note(
        self, repo_id: str, merge_request_id: int, body: str, discussion_id: str | None = None
    ):
        pass

    @abc.abstractmethod
    def job_log_trace(self, repo_id: str, job_id: int) -> str:
        pass

    @abc.abstractmethod
    def get_repository_archive(self, repo_id: str, commit_sha: str) -> AbstractContextManager[io.BytesIO]:
        pass

    @staticmethod
    @functools.cache
    def create_instance() -> AllRepoClient:
        """
        Get the repository client based on the configuration.

        Returns:
            The repository client instance.
        """
        if settings.CLIENT == ClientType.GITLAB:
            assert settings.GITLAB_AUTH_TOKEN is not None, "GitLab auth token is not set"
            return GitLabClient(auth_token=settings.GITLAB_AUTH_TOKEN.get_secret_value(), url=str(settings.GITLAB_URL))
        if settings.CLIENT == ClientType.GITHUB:
            raise NotImplementedError("GitHub client is not implemented yet")
        raise ValueError("Invalid repository client configuration")


class GitLabClient(RepoClient):
    """
    GitLab client to interact with GitLab repositories.
    """

    client: Gitlab
    client_slug = ClientType.GITLAB

    def __init__(self, auth_token: str, url: str | None = None):
        self.client = Gitlab(
            url=url, private_token=auth_token, timeout=10, keep_base_url=True, retry_transient_errors=True
        )

    @property
    def codebase_url(self) -> str:
        return self.client.url

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
            pk=cast("int", project.get_id()),
            slug=project.path_with_namespace,
            name=project.name,
            default_branch=project.default_branch,
            client=self.client_slug,
            topics=project.topics,
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
                pk=cast("int", project.get_id()),
                slug=project.path_with_namespace,
                name=project.name,
                default_branch=project.default_branch,
                client=self.client_slug,
                topics=project.topics,
            )
            for project in self.client.projects.list(
                all=load_all,
                iterator=True,
                archived=False,
                simple=True,
                membership=True,
                min_access_level=40,  # 40 is the access level for the maintainer role
                **optional_kwargs,
            )
        ]

    def get_repository_file(self, repo_id: str, file_path: str, ref: str) -> str | None:
        """
        Get the content of a file in a repository.

        Args:
            repo_id: The repository ID.
            file_path: The file path.
            ref: The branch or tag name.

        Returns:
            The content of the file. If the file is binary or not a text file, it returns None.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        try:
            project_file = project.files.get(file_path=file_path, ref=ref)
        except GitlabOperationError as e:
            if e.response_code == 404:
                return None
            raise e
        try:
            return project_file.decode().decode()
        except UnicodeDecodeError:
            return None

    def get_repository_file_link(self, repo_id: str, file_path: str, ref: str) -> str:
        """
        Get the link to a file in a repository.
        """
        return build_uri(self.codebase_url, f"/{repo_id}/-/blob/{ref}/{file_path}")

    async def get_project_uploaded_file(self, repo_id: str, file_path: str) -> bytes | None:
        """
        Download a markdown uploaded file from a repository.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        url = build_uri(self.codebase_url, f"/api/v4/projects/{project.get_id()}/{file_path}")
        return await async_download_url(url, headers={"PRIVATE-TOKEN": self.client.private_token})

    def repository_branch_exists(self, repo_id: str, branch: str) -> bool:
        """
        Check if a branch exists in a repository.

        Args:
            repo_id: The repository ID.
            branch: The branch name.

        Returns:
            True if the branch exists, otherwise False.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        try:
            project.branches.get(branch)
            return True
        except GitlabGetError:
            return False

    def set_repository_webhooks(
        self,
        repo_id: str,
        url: str,
        events: list[Literal["push_events", "issues_events", "note_events", "pipeline_events"]],
        push_events_branch_filter: str | None = None,
        enable_ssl_verification: bool = True,
        secret_token: str | None = None,
    ) -> bool:
        """
        Set webhooks for a repository.
        If the webhook already exists, it updates the existing one. Otherwise, it creates a new one.

        Args:
            repo_id: The repository ID.
            url: The webhook URL.
            events: The list of events to trigger the webhook.
            push_events_branch_filter: Filter to apply on branches for push events.
            enable_ssl_verification: Whether to enable SSL verification.
            secret_token: Secret token for webhook validation.

        Returns:
            True if the webhook was created, otherwise False.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        data = {
            "url": url,
            "name": BOT_NAME,
            "description": f"WebHooks for {BOT_NAME} integration.",
            "push_events": "push_events" in events,
            "merge_requests_events": "merge_requests_events" in events,
            "issues_events": "issues_events" in events,
            "pipeline_events": "pipeline_events" in events,
            "note_events": "note_events" in events,
            "job_events": "job_events" in events,
            "enable_ssl_verification": enable_ssl_verification,
            "push_events_branch_filter": push_events_branch_filter or "",
            "branch_filter_strategy": "wildcard" if push_events_branch_filter else "all_branches",
            "token": secret_token,
        }
        if project_hook := self._get_repository_hook_by_name(repo_id, data["name"]):
            for key, value in data.items():
                setattr(project_hook, key, value)
            project_hook.save()
            return False

        project.hooks.create(data)
        return True

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
        for hook in project.hooks.list(get_all=True, iterator=True):
            if hook.name == name:
                return cast("ProjectHook", hook)
        return None

    def get_commit_related_merge_requests(self, repo_id: str, commit_sha: str) -> list[MergeRequest]:
        """
        Get the related merge requests of a commit.

        Args:
            repo_id: The repository ID.
            commit_sha: The commit sha.

        Returns:
            The list of merge requests.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        return [
            MergeRequest(
                repo_id=repo_id,
                merge_request_id=cast("int", mr["iid"]),
                source_branch=mr["source_branch"],
                target_branch=mr["target_branch"],
                title=mr["title"],
                description=mr["description"],
                labels=mr["labels"],
            )
            for mr in project.commits.get(commit_sha).merge_requests()
        ]

    def get_merge_request(self, repo_id: str, merge_request_id: int) -> MergeRequest:
        """
        Get a merge request.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        mr = project.mergerequests.get(merge_request_id)
        return MergeRequest(
            repo_id=repo_id,
            merge_request_id=cast("int", mr.get_id()),
            source_branch=mr.source_branch,
            target_branch=mr.target_branch,
            title=mr.title,
            description=mr.description,
            labels=mr.labels,
            sha=mr.sha,
        )

    def get_merge_request_latest_pipeline(self, repo_id: str, merge_request_id: int) -> Pipeline | None:
        """
        Get the latest pipeline of a merge request.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id)
        try:
            pipeline = merge_request.pipelines.list(iterator=True, per_page=1).next()
        except StopIteration:
            return None
        # We need to get the object to get the jobs, otherwise the jobs are not loaded.
        pipeline_for_jobs = project.pipelines.get(id=pipeline.id, lazy=True)

        return Pipeline(
            id=pipeline.id,
            iid=pipeline.iid,
            status=pipeline.status,
            sha=pipeline.sha,
            web_url=pipeline.web_url,
            jobs=[
                Job(
                    id=job.id,
                    name=job.name,
                    status=job.status,
                    stage=job.stage,
                    allow_failure=job.allow_failure,
                    failure_reason=getattr(job, "failure_reason", None),
                )
                for job in pipeline_for_jobs.jobs.list(get_all=True, iterator=True)
            ],
        )

    def get_merge_request_diff(self, repo_id: str, merge_request_id: int) -> Generator[MergeRequestDiff]:
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
        assignee_id: int | None = None,
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
            assignee_id: The assignee ID.

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
                "assignee_id": assignee_id,
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
                merge_request.assignee_id = assignee_id
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
                action["content"] = cast("str", file_change.content)
            if file_change.action == "move":
                action["previous_path"] = cast("str", file_change.previous_path)
                # Move actions that do not specify content preserve the existing file content,
                # and any other value of content overwrites the file content.
                if file_change.content:
                    action["content"] = cast("str", file_change.content)
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
    def load_repo(self, repository: Repository, sha: str) -> Iterator[Path]:
        """
        Load a repository to a temporary directory.

        Args:
            repository: The repository.
            sha: The commit sha.

        Yields:
            The path to the repository directory.
        """
        project = self.client.projects.get(repository.slug, lazy=True)
        safe_sha = sha.replace("/", "_").replace(" ", "-")

        tmpdir = tempfile.TemporaryDirectory(prefix=f"{repository.pk}-{safe_sha}-repo")
        logger.debug("Loading repository to %s", tmpdir)

        try:
            with tempfile.NamedTemporaryFile(
                prefix=f"{repository.pk}-{safe_sha}-archive", suffix=".zip"
            ) as repo_archive:
                project.repository_archive(streamed=True, action=repo_archive.write, format="zip", sha=sha)
                repo_archive.flush()
                repo_archive.seek(0)

                with ZipFile(repo_archive.name, "r") as zipfile:
                    zipfile.extractall(path=tmpdir.name)
                    # The first file in the archive is the repository directory.
                    repo_dirname = zipfile.filelist[0].filename
        except:
            raise
        else:
            yield Path(tmpdir.name).joinpath(repo_dirname)
        finally:
            tmpdir.cleanup()

    def get_issue(self, repo_id: str, issue_id: int) -> Issue:
        """
        Get an issue.
        API documentation: https://docs.gitlab.com/ee/api/issues.html#single-issue

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.

        Returns:
            The issue object.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id)
        return Issue(
            id=issue.id,
            iid=issue.iid,
            title=issue.title,
            description=issue.description,
            state=issue.state,
            notes=self.get_issue_notes(repo_id, issue_id),
            labels=issue.labels,
            assignee=User(
                id=issue.assignee.get("id"), username=issue.assignee.get("username"), name=issue.assignee.get("name")
            )
            if issue.assignee
            else None,
            author=User(
                id=issue.author.get("id"), username=issue.author.get("username"), name=issue.author.get("name")
            ),
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

    def create_issue_note_emoji(self, repo_id: str, issue_id: int, emoji: str, note_id: str):
        """
        Create an emoji in a note of an issue.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id, lazy=True)
        note = issue.notes.get(note_id, lazy=True)
        note.awardemojis.create({"name": emoji})

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
            for note in issue.notes.list(get_all=True)
            if not note.system and not note.resolvable
        ]

    def get_issue_discussions(
        self, repo_id: str, issue_id: int, note_types: list[NoteType] | None = None
    ) -> list[Discussion]:  # noqa: A002
        """
        Get the discussions from a merge request.

        Args:
            repo_id: The repository ID.
            issue_id: The merge request ID.
            note_type: The note type.

        Returns:
            The list of discussions.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id, lazy=True)

        discussions = []
        for discussion in issue.discussions.list(get_all=True, iterator=True):
            if discussion.individual_note is False and (
                notes := self._serialize_notes(discussion.attributes["notes"], note_types)
            ):
                discussions.append(Discussion(id=discussion.id, notes=notes))
        return discussions

    def get_issue_discussion(
        self, repo_id: str, issue_id: int, discussion_id: str, only_resolvable: bool = True
    ) -> Discussion:
        """
        Get a discussion from an issue.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            discussion_id: The discussion ID.

        Returns:
            The discussion object.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id, lazy=True)
        discussion = issue.discussions.get(discussion_id)
        return Discussion(
            id=discussion.id,
            notes=self._serialize_notes(discussion.attributes["notes"], only_resolvable=only_resolvable),
        )

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
            MergeRequest(
                repo_id=repo_id,
                merge_request_id=cast("int", mr["iid"]),
                source_branch=mr["source_branch"],
                target_branch=mr["target_branch"],
                title=mr["title"],
                description=mr["description"],
                labels=mr["labels"],
            )
            for mr in issue.related_merge_requests(get_all=True)
            if (assignee_id is None or mr["assignee"] and mr["assignee"]["id"] == assignee_id)
            and (label is None or label in mr["labels"])
        ]

    def create_issue_discussion_note(self, repo_id: str, issue_id: int, body: str, discussion_id: str | None = None):
        """
        Create a note in a discussion of a issue.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            body: The note body.
            discussion_id: The discussion ID.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id, lazy=True)
        if discussion_id:
            discussion = issue.discussions.get(discussion_id, lazy=True)
            discussion.notes.create({"body": body})
        else:
            issue.discussions.create({"body": body})

    def delete_issue_discussion(self, repo_id: str, issue_id: int, discussion_id: str):
        """
        Delete a discussion in an issue.

        Args:
            repo_id: The repository ID.
            issue_id: The merge request ID.
            discussion_id: The discussion ID.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id, lazy=True)
        discussion = issue.discussions.get(discussion_id, lazy=True)
        for note in discussion.attributes["notes"]:
            discussion.notes.delete(note["id"])

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
        self, repo_id: str, merge_request_id: int, note_types: list[NoteType] | None = None
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
            for discussion in merge_request.discussions.list(get_all=True, iterator=True)
            if discussion.individual_note is False
            and (notes := self._serialize_notes(discussion.attributes["notes"], note_types))
        ]

    def get_merge_request_discussion(
        self, repo_id: str, merge_request_id: int, discussion_id: str, only_resolvable: bool = True
    ) -> Discussion:
        """
        Get a discussion from a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            discussion_id: The discussion ID.
            only_resolvable: Whether to only return resolvable notes.

        Returns:
            The discussion object.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        discussion = merge_request.discussions.get(discussion_id)
        return Discussion(
            id=discussion.id,
            notes=self._serialize_notes(discussion.attributes["notes"], only_resolvable=only_resolvable),
        )

    def _serialize_notes(
        self, notes: list[dict], note_types: list[NoteType] | None = None, only_resolvable: bool = True
    ) -> list[Note]:
        """
        Serialize dictionary of notes to Note objects.

        Args:
            notes: The list of notes.
            note_types: The list of note types.
            only_resolvable: Whether to only return resolvable notes.

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
                resolved=note.get("resolved"),
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
                )
                if "position" in note
                else None,
            )
            for note in notes
            if not note["system"]
            and (only_resolvable is False or note["resolvable"] and not note["resolved"])
            and (note_types is None or note["type"] in note_types)
        ]

    def update_merge_request_discussion_note(
        self,
        repo_id: str,
        merge_request_id: int,
        discussion_id: str,
        note_id: str,
        body: str,
        mark_as_resolved: bool = False,
    ):
        """
        Update a discussion in a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            discussion_id: The discussion ID.
            note_id: The note ID.
            body: The note body.
            mark_as_resolved: Whether to mark the note as resolved.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        discussion = merge_request.discussions.get(discussion_id, lazy=True)
        note = discussion.notes.get(note_id)
        note.body = body
        note.save()
        if mark_as_resolved:
            merge_request.discussions.update(discussion_id, {"resolved": True})

    def create_merge_request_note_emoji(self, repo_id: str, merge_request_id: int, emoji: str, note_id: str):
        """
        Create an emoji in a note of a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            emoji: The emoji name.
            note_id: The note ID.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        note = merge_request.notes.get(note_id, lazy=True)
        note.awardemojis.create({"name": emoji})

    def create_merge_request_discussion_note(
        self,
        repo_id: str,
        merge_request_id: int,
        body: str,
        discussion_id: str | None = None,
        mark_as_resolved: bool = False,
    ) -> str:
        """
        Create a note in a discussion of a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            body: The note body.
            discussion_id: The discussion ID.
            mark_as_resolved: Whether to mark the note as resolved.

        Returns:
            The note ID.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        if discussion_id:
            discussion = merge_request.discussions.get(discussion_id, lazy=True)
            note = discussion.notes.create({"body": body})
            if mark_as_resolved:
                merge_request.discussions.update(discussion_id, {"resolved": True})
            return note.id
        else:
            discussion = merge_request.discussions.create({"body": body})
            return discussion.attributes["notes"][0]["id"]

    def job_log_trace(self, repo_id: str, job_id: int) -> str:
        """
        Get the log trace of a job.

        Args:
            repo_id: The repository ID.
            job_id: The job ID.

        Returns:
            The log trace of the job.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        job = project.jobs.get(job_id, lazy=True)
        return job.trace().decode("utf-8")

    @contextmanager
    def get_repository_archive(self, repo_id: str, sha: str) -> AbstractContextManager[io.BytesIO]:  # type: ignore
        """
        Get the archive of a repository.

        Args:
            repo_id: The repository ID.
            sha: The commit sha.

        Yields:
            The archive of the repository.
        """
        tarstream = io.BytesIO()
        project = self.client.projects.get(repo_id)
        project.repository_archive(sha=sha, streamed=True, action=tarstream.write)

        tarstream.seek(0)
        yield tarstream
        tarstream.close()


class GitHubClient(RepoClient):
    """
    GitHub client to interact with GitHub repositories.

    Note: This class is not implemented yet. It is a placeholder for future development.
    """

    client_slug = ClientType.GITHUB


AllRepoClient = GitHubClient | GitLabClient
