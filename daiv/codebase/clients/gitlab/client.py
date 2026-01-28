from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

from git import Repo
from gitlab import Gitlab, GitlabCreateError, GitlabGetError, GitlabOperationError

from codebase.base import (
    Discussion,
    GitPlatform,
    Issue,
    Job,
    MergeRequest,
    Note,
    NoteDiffPosition,
    NotePosition,
    NotePositionLineRange,
    NoteType,
    Pipeline,
    Repository,
    User,
)
from codebase.clients import RepoClient
from core.constants import BOT_NAME
from core.utils import async_download_url, build_uri
from daiv import USER_AGENT

if TYPE_CHECKING:
    from collections.abc import Iterator

    from gitlab.v4.objects import ProjectHook

    from codebase.clients.base import Emoji

logger = logging.getLogger("daiv.clients")


class GitLabClient(RepoClient):
    """
    GitLab client to interact with GitLab repositories.
    """

    client: Gitlab
    git_platform = GitPlatform.GITLAB

    def __init__(self, auth_token: str, url: str | None = None):
        self.client = Gitlab(
            url=url,
            private_token=auth_token,
            timeout=10,
            keep_base_url=True,
            retry_transient_errors=True,
            user_agent=USER_AGENT,
        )

    @property
    def _codebase_url(self) -> str:
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
            clone_url=f"{self._codebase_url}/{project.path_with_namespace}.git",
            default_branch=project.default_branch,
            git_platform=self.git_platform,
            topics=project.topics,
        )

    def list_repositories(self, search: str | None = None, topics: list[str] | None = None) -> list[Repository]:
        """
        List all repositories.

        Args:
            search: The search query.
            topics: The topics to filter the repositories.

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
                clone_url=f"{self._codebase_url}/{project.path_with_namespace}.git",
                default_branch=project.default_branch,
                git_platform=self.git_platform,
                topics=project.topics,
            )
            for project in self.client.projects.list(
                all=True,
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

    async def get_project_uploaded_file(self, repo_id: str, file_path: str) -> bytes | None:
        """
        Download a markdown uploaded file from a repository.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        url = build_uri(self._codebase_url, f"/api/v4/projects/{project.get_id()}/{file_path}")
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
            "push_events": True,
            "issues_events": True,
            "note_events": True,
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
            web_url=mr.web_url,
            sha=mr.sha,
            author=User(id=mr.author.get("id"), username=mr.author.get("username"), name=mr.author.get("name")),
        )

    def get_merge_request_latest_pipelines(self, repo_id: str, merge_request_id: int) -> list[Pipeline]:
        """
        Get the latest pipelines of a merge request.
        For GitLab, we only have one pipeline per merge request.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id)
        try:
            pipeline = merge_request.pipelines.list(iterator=True, per_page=1).next()
        except StopIteration:
            return []
        # We need to get the object to get the jobs, otherwise the jobs are not loaded.
        pipeline_for_jobs = project.pipelines.get(id=pipeline.id, lazy=True)

        return [
            Pipeline(
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
        ]

    def update_or_create_merge_request(
        self,
        repo_id: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        labels: list[str] | None = None,
        assignee_id: int | None = None,
    ) -> MergeRequest:
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
            The merge request data.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        try:
            merge_request = project.mergerequests.create({
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
                "labels": labels or [],
                "assignee_id": assignee_id,
            })
            return MergeRequest(
                repo_id=repo_id,
                merge_request_id=cast("int", merge_request.get_id()),
                source_branch=merge_request.source_branch,
                target_branch=merge_request.target_branch,
                title=merge_request.title,
                description=merge_request.description,
                labels=merge_request.labels,
                web_url=merge_request.web_url,
                sha=merge_request.sha,
                author=User(
                    id=merge_request.author.get("id"),
                    username=merge_request.author.get("username"),
                    name=merge_request.author.get("name"),
                ),
            )
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
                return MergeRequest(
                    repo_id=repo_id,
                    merge_request_id=cast("int", merge_request.get_id()),
                    source_branch=merge_request.source_branch,
                    target_branch=merge_request.target_branch,
                    title=merge_request.title,
                    description=merge_request.description,
                    labels=merge_request.labels,
                    web_url=merge_request.web_url,
                    sha=merge_request.sha,
                    author=User(
                        id=merge_request.author.get("id"),
                        username=merge_request.author.get("username"),
                        name=merge_request.author.get("name"),
                    ),
                )
            raise e

    @contextmanager
    def load_repo(self, repository: Repository, sha: str) -> Iterator[Repo]:
        """
        Clone a repository to a temporary directory.

        Args:
            repository: The repository.
            sha: The commit sha.

        Yields:
            The repository object cloned to the temporary directory.
        """
        from codebase.clients.utils import safe_slug

        with tempfile.TemporaryDirectory(prefix=f"{safe_slug(repository.slug)}-{repository.pk}") as tmpdir:
            logger.debug("Cloning repository %s to %s", repository.clone_url, tmpdir)

            parsed = urlparse(repository.clone_url)
            clone_url = f"{parsed.scheme}://oauth2:{self.client.private_token}@{parsed.netloc}{parsed.path}"

            clone_dir = Path(tmpdir) / "repo"
            clone_dir.mkdir(exist_ok=True)
            yield Repo.clone_from(clone_url, clone_dir, branch=sha)

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
            notes=self._get_issue_notes(repo_id, issue_id),
            labels=issue.labels,
            assignee=User(
                id=issue.assignee.get("id"), username=issue.assignee.get("username"), name=issue.assignee.get("name")
            )
            if issue.assignee
            else None,
            author=User(
                id=issue.author.get("id"), username=issue.author.get("username"), name=issue.author.get("name")
            ),
        )

    def create_issue(self, repo_id: str, title: str, description: str, labels: list[str] | None = None) -> int:
        """
        Create an issue in a repository.
        API documentation: https://docs.gitlab.com/ee/api/issues.html#new-issue

        Args:
            repo_id: The repository ID.
            title: The issue title.
            description: The issue description.
            labels: Optional list of labels to apply to the issue.

        Returns:
            The created issue IID.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue_data = {"title": title, "description": description}
        if labels:
            issue_data["labels"] = ",".join(labels)
        issue = project.issues.create(issue_data)
        return issue.iid

    def create_issue_emoji(self, repo_id: str, issue_id: int, emoji: Emoji, note_id: str | None = None):
        """
        Create an emoji direclty on an issue or on an issue note.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id, lazy=True)
        if note_id is not None:
            note = issue.notes.get(note_id, lazy=True)
            note.awardemojis.create({"name": emoji})
        else:
            issue.awardemojis.create({"name": emoji})

    def _get_issue_notes(self, repo_id: str, issue_id: int) -> list[Note]:
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

    def get_issue_comment(self, repo_id: str, issue_id: int, comment_id: str) -> Discussion:
        """
        Get a comment from an issue.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            comment_id: The comment ID.

        Returns:
            The discussion object.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id, lazy=True)
        discussion = issue.discussions.get(comment_id)
        return Discussion(
            id=comment_id, notes=self._serialize_notes(discussion.attributes["notes"], [None], only_resolvable=False)
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
                web_url=mr.get("web_url"),
                author=User(
                    id=mr.get("author").get("id"),
                    username=mr.get("author").get("username"),
                    name=mr.get("author").get("name"),
                ),
            )
            for mr in issue.related_merge_requests(get_all=True)
            if (assignee_id is None or mr["assignee"] and mr["assignee"]["id"] == assignee_id)
            and (label is None or label in mr["labels"])
        ]

    def create_issue_comment(
        self, repo_id: str, issue_id: int, body: str, reply_to_id: str | None = None, as_thread: bool = False
    ) -> str | None:
        """
        Comment on an issue.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            body: The comment body.

        Returns:
            The comment ID.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id, lazy=True)
        if reply_to_id:
            discussion = issue.discussions.get(reply_to_id, lazy=True)
            return discussion.notes.create({"body": body}).id
        elif as_thread:
            discussion = issue.discussions.create({"body": body})
            return discussion.attributes["notes"][0]["id"]
        return issue.notes.create({"body": body}).id

    def update_issue_comment(
        self, repo_id: str, issue_id: int, comment_id: int, body: str, reply_to_id: str | None = None
    ):
        """
        Update a comment on an issue.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            comment_id: The comment ID.
            body: The comment body.
            reply_to_id: The ID of the comment to reply to.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id, lazy=True)
        if reply_to_id:
            discussion = issue.discussions.get(reply_to_id, lazy=True)
            comment = discussion.notes.get(comment_id)
        else:
            comment = issue.notes.get(comment_id)
        comment.body = body
        comment.save()

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

    def get_merge_request_review_comments(self, repo_id: str, merge_request_id: int) -> list[Discussion]:
        """
        Get the review comments left on the merge request diff.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        return [
            Discussion(id=discussion.id, notes=notes, is_thread=True, is_resolvable=True, resolve_id=discussion.id)
            for discussion in merge_request.discussions.list(get_all=True, iterator=True)
            if discussion.individual_note is False
            and (
                notes := self._serialize_notes(
                    discussion.attributes["notes"], [NoteType.DIFF_NOTE, NoteType.DISCUSSION_NOTE]
                )
            )
        ]

    def get_merge_request_comments(self, repo_id: str, merge_request_id: int) -> list[Discussion]:
        """
        Get the comments done directly on a merge request (not in a review thread).
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        return [
            Discussion(id=discussion.id, notes=notes)
            for discussion in merge_request.discussions.list(get_all=True, iterator=True)
            if discussion.individual_note is True
            and (notes := self._serialize_notes(discussion.attributes["notes"], [None], only_resolvable=False))
        ]

    def get_merge_request_comment(self, repo_id: str, merge_request_id: int, comment_id: str) -> Discussion:
        """
        Get a comment from a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            comment_id: The comment ID.

        Returns:
            The discussion object.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        discussion = merge_request.discussions.get(comment_id)
        return Discussion(
            id=discussion.id, notes=self._serialize_notes(discussion.attributes["notes"], [None], only_resolvable=False)
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

    def create_merge_request_comment(
        self,
        repo_id: str,
        merge_request_id: int,
        body: str,
        reply_to_id: str | None = None,
        as_thread: bool = False,
        mark_as_resolved: bool = False,
    ) -> str | None:
        """
        Comment on a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            body: The comment body.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        to_return = None

        if reply_to_id:
            discussion = merge_request.discussions.get(reply_to_id, lazy=True)
            to_return = discussion.notes.create({"body": body}).id

            if mark_as_resolved:
                self.mark_merge_request_comment_as_resolved(repo_id, merge_request_id, reply_to_id)

        elif as_thread:
            discussion = merge_request.discussions.create({"body": body})
            note_id = discussion.attributes["notes"][0]["id"]
            to_return = note_id
        else:
            to_return = merge_request.notes.create({"body": body}).id

        return to_return

    def create_merge_request_note_emoji(self, repo_id: str, merge_request_id: int, emoji: Emoji, note_id: str):
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
        try:
            note.awardemojis.create({"name": emoji})
        except GitlabCreateError as e:
            if e.response_code == 404 and "Award Emoji Name has already been taken" in e.error_message:
                pass
            else:
                raise e

    def mark_merge_request_comment_as_resolved(self, repo_id: str, merge_request_id: int, discussion_id: str):
        """
        Mark a review as resolved.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        merge_request.discussions.update(discussion_id, {"resolved": True})

    def get_job(self, repo_id: str, job_id: int):
        """
        Get a job by its ID.

        Args:
            repo_id: The repository ID.
            job_id: The job ID.

        Returns:
            Job object with job details.
        """
        from codebase.base import Job

        project = self.client.projects.get(repo_id, lazy=True)
        job = project.jobs.get(job_id)

        return Job(
            id=job.id,
            name=job.name,
            status=job.status,
            stage=job.stage,
            allow_failure=job.allow_failure,
            failure_reason=getattr(job, "failure_reason", None),
        )

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
