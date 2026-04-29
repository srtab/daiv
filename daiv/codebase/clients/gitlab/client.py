from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from git import Repo
from gitlab import Gitlab, GitlabCreateError, GitlabOperationError
from gitlab.exceptions import GitlabError

from codebase.base import (
    Discussion,
    GitPlatform,
    Issue,
    MergeRequest,
    MergeRequestCommit,
    MergeRequestDiffStats,
    Note,
    NoteDiffPosition,
    NotePosition,
    NotePositionLineRange,
    NoteType,
    Repository,
    User,
)
from codebase.clients import RepoClient
from core.constants import BOT_NAME
from core.utils import async_download_url, build_uri
from daiv import USER_AGENT

if TYPE_CHECKING:
    from collections.abc import Iterator

    from gitlab.v4.objects import ProjectHook, ProjectMergeRequest

    from codebase.clients.base import Emoji, WebhookSetupResult

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

    def _get_commit_email(self) -> str:
        """
        Resolve the best available email for commit attribution.
        """
        self.client.auth()
        if user := self.client.user:
            for email_attr in ("commit_email", "public_email", "email"):
                if (email := getattr(user, email_attr, None)) and isinstance(email, str) and email.strip():
                    return email
            return f"{user.username}@users.noreply.gitlab.com"

        return f"{self.current_user.username}@users.noreply.gitlab.com"

    def _configure_commit_identity(self, repo: Repo) -> None:
        """
        Configure repository-local git identity to match the GitLab bot user.
        """
        bot_username = self.current_user.username
        bot_email = self._get_commit_email()

        with repo.config_writer() as writer:
            writer.set_value("user", "name", bot_username)
            writer.set_value("user", "email", bot_email)

    # Repository
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
            clone_url=f"{self.client.url}/{project.path_with_namespace}.git",
            html_url=project.web_url,
            default_branch=project.default_branch,
            git_platform=self.git_platform,
            topics=project.topics,
        )

    def list_repositories(
        self, search: str | None = None, topics: list[str] | None = None, limit: int | None = None
    ) -> list[Repository]:
        """
        List all repositories.

        Args:
            search: The search query.
            topics: The topics to filter the repositories.
            limit: Maximum number of repositories to return. None means no limit.

        Returns:
            The list of repositories.
        """
        optional_kwargs: dict[str, Any] = {}
        if search:
            optional_kwargs["search"] = search
            optional_kwargs["search_namespaces"] = True
        if topics:
            optional_kwargs["topic"] = ",".join(topics)
        if limit is not None:
            optional_kwargs["per_page"] = min(limit, 100)

        repos: list[Repository] = []
        for project in self.client.projects.list(
            iterator=True,
            archived=False,
            simple=True,
            membership=True,
            min_access_level=40,  # 40 is the access level for the maintainer role
            order_by="last_activity_at",
            sort="desc",
            **optional_kwargs,
        ):
            repos.append(
                Repository(
                    pk=cast("int", project.get_id()),
                    slug=project.path_with_namespace,
                    name=project.name,
                    clone_url=f"{self.client.url}/{project.path_with_namespace}.git",
                    html_url=project.web_url,
                    default_branch=project.default_branch,
                    git_platform=self.git_platform,
                    topics=project.topics,
                )
            )
            if limit is not None and len(repos) >= limit:
                break
        return repos

    def list_branches(self, repo_id: str, search: str | None = None, limit: int = 20) -> list[str]:
        """
        Return up to ``limit`` branch names, optionally filtered by server-side substring ``search``.
        Branches are ordered by commit recency (most recent first).
        """
        project = self.client.projects.get(repo_id, lazy=True)
        kwargs: dict[str, Any] = {"per_page": min(limit, 100), "sort": "updated_desc"}
        if search:
            kwargs["search"] = search
        names: list[str] = []
        for branch in project.branches.list(iterator=True, **kwargs):
            names.append(branch.name)
            if len(names) >= limit:
                break
        return names

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
        url = build_uri(self.client.url, f"/api/v4/projects/{project.get_id()}/{file_path}")
        return await async_download_url(url, headers={"PRIVATE-TOKEN": self.client.private_token})

    def set_repository_webhooks(
        self,
        repo_id: str,
        url: str,
        push_events_branch_filter: str | None = None,
        enable_ssl_verification: bool = True,
        secret_token: str | None = None,
        update: bool = False,
    ) -> WebhookSetupResult:
        """
        Set webhooks for a repository.

        Args:
            repo_id: The repository ID.
            url: The webhook URL.
            push_events_branch_filter: Filter to apply on branches for push events.
            enable_ssl_verification: Whether to enable SSL verification.
            secret_token: Secret token for webhook validation.
            update: Whether to update existing webhooks. If False, existing webhooks are skipped.

        Returns:
            The result of the webhook setup operation.
        """
        from codebase.clients.base import WebhookSetupResult

        project = self.client.projects.get(repo_id, lazy=True)
        data = {
            "url": url,
            "name": BOT_NAME,
            "description": f"WebHooks for {BOT_NAME} integration.",
            "push_events": True,
            "issues_events": True,
            "note_events": True,
            "merge_requests_events": True,
            "enable_ssl_verification": enable_ssl_verification,
            "push_events_branch_filter": push_events_branch_filter or "",
            "branch_filter_strategy": "wildcard" if push_events_branch_filter else "all_branches",
            "token": secret_token,
        }
        if project_hook := self._get_repository_hook_by_name(repo_id, data["name"]):
            if not update:
                return WebhookSetupResult.SKIPPED
            for key, value in data.items():
                setattr(project_hook, key, value)
            project_hook.save()
            return WebhookSetupResult.UPDATED

        project.hooks.create(data)
        return WebhookSetupResult.CREATED

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
            repo = Repo.clone_from(clone_url, clone_dir, branch=sha)
            self._configure_commit_identity(repo)
            yield repo

    # Issue
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

    def create_issue_emoji(self, repo_id: str, issue_id: int, emoji: Emoji, note_id: int | None = None):
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

    def has_issue_reaction(self, repo_id: str, issue_id: int, emoji: Emoji) -> bool:
        """
        Check if an issue has a specific emoji reaction from the current user.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            emoji: The emoji to check for.

        Returns:
            True if the issue has the reaction, False otherwise.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        issue = project.issues.get(issue_id, lazy=True)
        current_user_id = self.current_user.id

        for award_emoji in issue.awardemojis.list(iterator=True):
            if award_emoji.name == emoji and award_emoji.user["id"] == current_user_id:
                return True

        return False

    # Merge request
    def update_or_create_merge_request(
        self,
        repo_id: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        labels: list[str] | None = None,
        assignee_id: str | int | None = None,
        as_draft: bool = False,
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
            as_draft: Whether to create the merge request as a draft.

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
                "work_in_progress": as_draft,
            })
            return self._serialize_merge_request(repo_id, merge_request)
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
                merge_request.work_in_progress = as_draft
                merge_request.save()
                return self._serialize_merge_request(repo_id, merge_request)
            raise e

    def get_merge_request_by_branches(
        self, repo_id: str, source_branch: str, target_branch: str
    ) -> MergeRequest | None:
        """
        Return the open merge request for this source/target branch pair, or ``None``.

        Args:
            repo_id: The repository ID.
            source_branch: The source branch.
            target_branch: The target branch.

        Returns:
            The merge request if one open MR matches, otherwise ``None``.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_requests = project.mergerequests.list(
            source_branch=source_branch, target_branch=target_branch, state="opened", iterator=True
        )
        merge_request = next(merge_requests, None)
        if merge_request is None:
            return None
        return self._serialize_merge_request(repo_id, merge_request)

    def update_merge_request(
        self,
        repo_id: str,
        merge_request_id: int,
        as_draft: bool | None = None,
        title: str | None = None,
        description: str | None = None,
        labels: list[str] | None = None,
        assignee_id: str | int | None = None,
    ) -> MergeRequest:
        """
        Update an existing merge request if it has changes.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            as_draft: Whether to set the merge request as a draft.
            title: The title of the merge request.
            description: The description of the merge request.
            labels: The labels of the merge request.
            assignee_id: The assignee ID of the merge request.

        Returns:
            The merge request.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id)

        has_changes = False
        if as_draft is not None and merge_request.work_in_progress != as_draft:
            merge_request.work_in_progress = as_draft
            has_changes = True
        if title is not None and merge_request.title != title:
            merge_request.title = title
            has_changes = True
        if description is not None and merge_request.description != description:
            merge_request.description = description
            has_changes = True
        if labels is not None and any(label.title not in labels for label in merge_request.labels):
            mr_label_titles = [label.title for label in merge_request.labels]
            merge_request.labels += [label for label in labels if label not in mr_label_titles]
            has_changes = True
        if assignee_id is not None and merge_request.assignee_id != assignee_id:
            merge_request.assignee_id = assignee_id
            has_changes = True

        if has_changes:
            merge_request.save()

        return self._serialize_merge_request(repo_id, merge_request)

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

    def get_merge_request_diff_stats(self, repo_id: str, merge_request_id: int) -> MergeRequestDiffStats:
        """
        Get diff statistics for a merge request by parsing the diff content.

        GitLab's MR API does not expose aggregate line counts, so stats are computed
        by parsing the unified diff. Note that GitLab may truncate diffs for very
        large MRs, so stats may be incomplete in those cases.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request IID.

        Returns:
            The diff statistics (lines added, lines removed, files changed).
        """
        project = self.client.projects.get(repo_id, lazy=True)
        mr = project.mergerequests.get(merge_request_id)
        changes = mr.changes()

        if changes.get("overflow"):
            logger.warning("Diff changes overflow for %s!%d — stats will be incomplete", repo_id, merge_request_id)

        lines_added = 0
        lines_removed = 0
        file_changes = changes.get("changes", [])
        for change in file_changes:
            diff_text = change.get("diff", "")
            for line in diff_text.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    lines_added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    lines_removed += 1

        return MergeRequestDiffStats(
            lines_added=lines_added, lines_removed=lines_removed, files_changed=len(file_changes)
        )

    def get_merge_request_commits(self, repo_id: str, merge_request_id: int) -> list[MergeRequestCommit]:
        """
        Get the pre-squash commit list for a merge request with per-commit stats.

        Uses N+1 API calls: 1 for the commit list + 1 per commit for stats.
        Caps at 100 commits to avoid excessive API usage.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request IID.

        Returns:
            List of commits with author email and line stats.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        mr = project.mergerequests.get(merge_request_id)
        commits = list(mr.commits(get_all=True))

        if len(commits) > 100:
            logger.warning("MR %s!%d has %d commits, capping at 100 for stats", repo_id, merge_request_id, len(commits))
            commits = commits[:100]

        result: list[MergeRequestCommit] = []
        for commit_ref in commits:
            try:
                full_commit = project.commits.get(commit_ref.id)
                stats = full_commit.stats or {}
            except GitlabError:
                logger.warning(
                    "Failed to fetch stats for commit %s in %s!%d, skipping", commit_ref.id, repo_id, merge_request_id
                )
                stats = {}
            result.append(
                MergeRequestCommit(
                    sha=commit_ref.id,
                    author_email=commit_ref.author_email or "",
                    lines_added=stats.get("additions", 0),
                    lines_removed=stats.get("deletions", 0),
                )
            )
        return result

    def get_bot_commit_email(self) -> str:
        """
        Return the email address DAIV uses when authoring commits on GitLab.
        """
        return self._get_commit_email()

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

    def create_merge_request_note_emoji(self, repo_id: str, merge_request_id: int, emoji: Emoji, note_id: int):
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

    def create_merge_request_inline_discussion(
        self, repo_id: str, merge_request_id: int, body: str, position: dict[str, Any]
    ) -> str:
        """
        Create an inline diff discussion on a merge request via the Python API.

        Uses python-gitlab's dict-based payload rather than the CLI so that the
        nested `position` hash (position_type, base_sha, start_sha, head_sha,
        old_path, new_path, line anchors) is serialised correctly by the library.

        Args:
            repo_id: The repository ID (slug).
            merge_request_id: The merge request IID.
            body: The discussion body text.
            position: The diff position dict accepted by the GitLab Discussions API.

        Returns:
            The created discussion ID string.
        """
        project = self.client.projects.get(repo_id, lazy=True)
        merge_request = project.mergerequests.get(merge_request_id, lazy=True)
        discussion = merge_request.discussions.create({"body": body, "position": position})
        return discussion.id

    # User
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

    def _serialize_merge_request(self, repo_id: str, merge_request: ProjectMergeRequest) -> MergeRequest:
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
            draft=merge_request.work_in_progress,
        )
