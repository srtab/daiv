from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from git import Repo
from github import Github, GithubIntegration, Installation, UnknownObjectException
from github.GithubException import GithubException
from github.IssueComment import IssueComment
from github.PullRequestComment import PullRequestComment

from codebase.base import (
    Discussion,
    GitPlatform,
    Issue,
    MergeRequest,
    MergeRequestCommit,
    MergeRequestDiffStats,
    Note,
    NoteableType,
    NoteDiffPosition,
    NoteDiffPositionType,
    NotePosition,
    NotePositionLineRange,
    NotePositionType,
    NoteType,
    Repository,
    User,
)
from codebase.clients import RepoClient
from codebase.clients.base import Emoji, WebhookSetupResult
from core.utils import async_download_url

if TYPE_CHECKING:
    from collections.abc import Iterator


logger = logging.getLogger("daiv.clients")

EMOJI_MAP = {Emoji.THUMBSUP: "+1", Emoji.EYES: "eyes"}


class GitHubClient(RepoClient):
    """
    GitHub client to interact with GitHub repositories.
    """

    client: Github
    client_installation: Installation.Installation
    git_platform = GitPlatform.GITHUB

    def __init__(self, integration: GithubIntegration, installation_id: int):
        self._integration = integration
        self.client_installation = integration.get_app_installation(installation_id)
        self.client = self.client_installation.get_github_for_installation()

    def _configure_commit_identity(self, repo: Repo) -> None:
        """
        Configure repository-local git identity to match the GitHub App bot user.
        """
        bot_login = f"{self.client_installation.app_slug}[bot]"
        bot_user_id = self.current_user.id
        bot_email = f"{bot_user_id}+{bot_login}@users.noreply.github.com"

        with repo.config_writer() as writer:
            writer.set_value("user", "name", bot_login)
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
        repo = self.client.get_repo(repo_id)
        return Repository(
            pk=repo.id,
            slug=repo.full_name,
            name=repo.name,
            clone_url=repo.clone_url,
            html_url=repo.html_url,
            default_branch=repo.default_branch,
            git_platform=self.git_platform,
            topics=repo.topics,
        )

    def list_repositories(
        self, search: str | None = None, topics: list[str] | None = None, limit: int | None = None
    ) -> list[Repository]:
        """
        List all repositories.

        Args:
            search: The search query (in-memory name/slug match).
            topics: The topics to filter the repositories.
            limit: Maximum number of repositories to return. None means no limit.

        Returns:
            The list of repositories.
        """
        repos: list[Repository] = []
        for repo in self.client_installation.get_repos():
            if topics is not None and not any(topic in repo.topics for topic in topics):
                continue
            repos.append(
                Repository(
                    pk=repo.id,
                    slug=repo.full_name,
                    name=repo.name,
                    default_branch=repo.default_branch,
                    git_platform=self.git_platform,
                    topics=repo.topics,
                    clone_url=repo.clone_url,
                    html_url=repo.html_url,
                )
            )
            # Break early only when no search filter needs full scan
            if limit is not None and search is None and len(repos) >= limit:
                break
        if search:
            search_lower = search.lower()
            repos = [r for r in repos if search_lower in r.name.lower() or search_lower in r.slug.lower()]
            if limit is not None:
                repos = repos[:limit]
        return repos

    def list_branches(self, repo_id: str, search: str | None = None, limit: int = 20) -> list[str]:
        """
        Return up to ``limit`` branch names. GitHub's branches endpoint has no server-side
        search, so we filter client-side; PyGithub paginates lazily so we don't fetch more
        pages than needed when ``limit`` is reached.
        """
        repo = self.client.get_repo(repo_id, lazy=True)
        needle = search.lower() if search else None
        names: list[str] = []
        for branch in repo.get_branches():
            if needle is not None and needle not in branch.name.lower():
                continue
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
        repo = self.client.get_repo(repo_id, lazy=True)
        try:
            content = repo.get_contents(file_path, ref=ref)
        except GithubException as e:
            error_message = str(getattr(e, "data", {}).get("message", "")).lower()
            if e.status == 404 and ("repository is empty" in error_message or "not found" in error_message):
                return None
            raise
        try:
            return content.decoded_content.decode()
        except UnicodeDecodeError:
            return None

    async def get_project_uploaded_file(self, repo_id: str, file_path: str) -> bytes | None:
        """
        Download a user-attachments file from GitHub.

        Args:
            repo_id: The repository ID (not used for GitHub, as file_path contains full URL).
            file_path: The full URL to the GitHub user-attachments file.

        Returns:
            The file content as bytes, or None if the download fails.
        """
        token = self.client.requester.auth.token
        return await async_download_url(file_path, headers={"Authorization": f"Bearer {token}"})

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
        """
        events = ["push", "issues", "pull_request_review", "issue_comment", "pull_request"]
        config = {
            "url": url,
            "content_type": "json",
            "secret": secret_token,
            "insecure_ssl": not enable_ssl_verification,
        }
        repo = self.client.get_repo(repo_id, lazy=True)

        for hook in repo.get_hooks():
            if hook.url == url:
                if not update:
                    return WebhookSetupResult.SKIPPED
                hook.edit("web", config, events, active=True)
                return WebhookSetupResult.UPDATED

        repo.create_hook("web", config, events, active=True)
        return WebhookSetupResult.CREATED

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
            # the access token is valid for 1 hour
            access_token = self._integration.get_access_token(
                self.client_installation.id, permissions={"contents": "write"}
            )
            parsed = urlparse(repository.clone_url)
            clone_url = f"{parsed.scheme}://oauth2:{access_token.token}@{parsed.netloc}{parsed.path}"
            clone_dir = Path(tmpdir) / "repo"
            clone_dir.mkdir(exist_ok=True)
            repo = Repo.clone_from(clone_url, clone_dir, branch=sha)
            self._configure_commit_identity(repo)
            yield repo

    # Issue
    def get_issue(self, repo_id: str, issue_id: int) -> Issue:
        """
        Get an issue.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.

        Returns:
            The issue object.
        """
        repo = self.client.get_repo(repo_id, lazy=True)
        issue = repo.get_issue(issue_id)
        return Issue(
            id=issue.id,
            iid=issue.number,
            title=issue.title,
            description=issue.body,
            state=issue.state,
            assignee=User(id=issue.assignee.id, username=issue.assignee.login, name=issue.assignee.name)
            if issue.assignee
            else None,
            author=User(id=issue.user.id, username=issue.user.login, name=issue.user.name) if issue.user else None,
            notes=self._serialize_comments(issue.get_comments()),
            labels=[label.name for label in issue.labels],
        )

    def create_issue(self, repo_id: str, title: str, description: str, labels: list[str] | None = None) -> int:
        """
        Create an issue in a repository.

        Args:
            repo_id: The repository ID.
            title: The issue title.
            description: The issue description.
            labels: Optional list of labels to apply to the issue.

        Returns:
            The created issue number.
        """
        repo = self.client.get_repo(repo_id, lazy=True)
        issue = repo.create_issue(title=title, body=description, labels=labels or [])
        return issue.number

    def get_issue_comment(self, repo_id: str, issue_id: int, comment_id: str) -> Discussion:
        """
        Get a comment from an issue.

        For GitHub, there's no distinction between comments and notes.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            comment_id: The comment ID.

        Returns:
            The discussion object.
        """
        issue = self.client.get_repo(repo_id, lazy=True).get_issue(issue_id)
        comment = issue.get_comment(int(comment_id))
        # GitHub doesn't have discussions like GitLab. This is a workaround to get the notes of an issue.
        return Discussion(id=str(comment_id), notes=self._serialize_comments([comment]))

    def create_issue_comment(
        self, repo_id: str, issue_id: int, body: str, reply_to_id: str | None = None, as_thread: bool = False
    ) -> str | None:
        """
        Comment on an issue.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            body: The comment body.
            reply_to_id: The ID of the comment to reply to. This is not supported for GitHub.
            as_thread: Whether to create a thread. This is not supported for GitHub.

        Returns:
            The comment ID.
        """
        return self.client.get_repo(repo_id, lazy=True).get_issue(issue_id).create_comment(body).id

    def create_issue_emoji(self, repo_id: str, issue_id: int, emoji: Emoji, note_id: int | None = None):
        """
        Create an emoji in a note of an issue.
        """
        if not (emoji_reaction := EMOJI_MAP.get(emoji)):
            raise ValueError(f"Unsupported emoji: {emoji}")

        issue = self.client.get_repo(repo_id, lazy=True).get_issue(issue_id)
        if note_id is not None:
            issue.get_comment(note_id).create_reaction(emoji_reaction)
        else:
            issue.create_reaction(emoji_reaction)

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
        if not (emoji_reaction := EMOJI_MAP.get(emoji)):
            raise ValueError(f"Unsupported emoji: {emoji}")

        issue = self.client.get_repo(repo_id, lazy=True).get_issue(issue_id)
        current_user_id = self.current_user.id

        for reaction in issue.get_reactions():
            if reaction.content == emoji_reaction and reaction.user.id == current_user_id:
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
        Create a merge request or update an existing one if it already exists based on the source and target branches.

        Args:
            repo_id: The repository ID.
            source_branch: The source branch.
            target_branch: The target branch.
            title: The title.
            description: The description.
            labels: The labels.
            assignee_id: The assignee ID.
            as_draft: Whether to create the merge request as a draft.

        Returns:
            The merge request data.
        """
        repo = self.client.get_repo(repo_id, lazy=True)

        try:
            pr = repo.create_pull(base=target_branch, head=source_branch, title=title, body=description, draft=as_draft)
        except GithubException as e:
            if e.status != 422 or not any(
                error.get("message").startswith("A pull request already exists for")
                for error in e.data.get("errors", [])
            ):
                raise e

            prs = repo.get_pulls(base=target_branch, head=source_branch, state="open")

            if not prs:
                raise e

            pr = prs[0]
            pr.edit(title=title, body=description)

            if pr.draft and not as_draft:
                pr.mark_ready_for_review()
            elif not pr.draft and as_draft:
                pr.convert_to_draft()

        if labels is not None and not any(label.name in labels for label in pr.labels):
            pr.add_to_labels(*labels)

        if assignee_id and not any(assignee.id == assignee_id for assignee in pr.assignees):
            pr.add_to_assignees(assignee_id)

        return MergeRequest(
            repo_id=repo_id,
            merge_request_id=pr.number,
            source_branch=pr.head.ref,
            target_branch=pr.base.ref,
            title=pr.title,
            description=pr.body or "",
            labels=[label.name for label in pr.labels],
            web_url=pr.html_url,
            sha=pr.head.sha,
            author=User(id=pr.user.id, username=pr.user.login, name=pr.user.name),
            draft=pr.draft,
        )

    def get_merge_request_by_branches(
        self, repo_id: str, source_branch: str, target_branch: str
    ) -> MergeRequest | None:
        """
        Return the open pull request for this source/target branch pair, or ``None``.

        Args:
            repo_id: The repository ID.
            source_branch: The source branch.
            target_branch: The target branch.

        Returns:
            The pull request if one open PR matches, otherwise ``None``.
        """
        repo = self.client.get_repo(repo_id, lazy=True)
        prs = repo.get_pulls(state="open", base=target_branch, head=source_branch)
        pr = next(iter(prs), None)
        if pr is None:
            return None
        return MergeRequest(
            repo_id=repo_id,
            merge_request_id=pr.number,
            source_branch=pr.head.ref,
            target_branch=pr.base.ref,
            title=pr.title,
            description=pr.body or "",
            labels=[label.name for label in pr.labels],
            web_url=pr.html_url,
            sha=pr.head.sha,
            author=User(id=pr.user.id, username=pr.user.login, name=pr.user.name),
            draft=pr.draft,
        )

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
        repo = self.client.get_repo(repo_id, lazy=True)
        pr = repo.get_pull(merge_request_id)

        if as_draft is not None and pr.draft and not as_draft:
            pr.mark_ready_for_review()
        elif as_draft is not None and not pr.draft and as_draft:
            pr.convert_to_draft()

        edit_fields = {}
        if title is not None:
            edit_fields["title"] = title

        if description is not None:
            edit_fields["body"] = description

        if edit_fields:
            pr.edit(**edit_fields)

        if labels is not None and not any(label.name in labels for label in pr.labels):
            pr.add_to_labels(*labels)

        if assignee_id is not None and not any(assignee.id == assignee_id for assignee in pr.assignees):
            pr.add_to_assignees(assignee_id)

        return MergeRequest(
            repo_id=repo_id,
            merge_request_id=pr.number,
            source_branch=pr.head.ref,
            target_branch=pr.base.ref,
            title=pr.title,
            description=pr.body or "",
            labels=[label.name for label in pr.labels],
            web_url=pr.html_url,
            sha=pr.head.sha,
            author=User(id=pr.user.id, username=pr.user.login, name=pr.user.name),
            draft=pr.draft,
        )

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
            reply_to_id: The ID of the comment to reply to.
            as_thread: Whether to create a thread.
            mark_as_resolved: Whether to mark the comment as resolved.

        Returns:
            The ID of the comment.
        """
        pr = self.client.get_repo(repo_id, lazy=True).get_pull(merge_request_id)
        to_return = None

        if reply_to_id:
            to_return = pr.create_review_comment_reply(int(reply_to_id), body).id

            if mark_as_resolved:
                self.mark_merge_request_comment_as_resolved(repo_id, merge_request_id, reply_to_id)

        elif as_thread:
            # The only threadable comments are review comments.
            raise NotImplementedError("Not implemented for GitHub")
        else:
            to_return = pr.create_issue_comment(body).id
        return to_return

    def get_merge_request_diff_stats(self, repo_id: str, merge_request_id: int) -> MergeRequestDiffStats:
        """
        Get diff statistics for a pull request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The pull request number.

        Returns:
            The diff statistics (lines added, lines removed, files changed).
        """
        repo = self.client.get_repo(repo_id, lazy=True)
        pr = repo.get_pull(merge_request_id)
        return MergeRequestDiffStats(
            lines_added=pr.additions, lines_removed=pr.deletions, files_changed=pr.changed_files
        )

    def get_merge_request_commits(self, repo_id: str, merge_request_id: int) -> list[MergeRequestCommit]:
        """
        Get the pre-squash commit list for a pull request with per-commit stats.

        Accessing ``commit.stats`` triggers a lazy per-commit API call, so this
        is effectively N+1 requests (same pattern as GitLab). GitHub limits
        PR commits to 250, so no explicit cap is applied.

        Args:
            repo_id: The repository ID.
            merge_request_id: The pull request number.

        Returns:
            List of commits with author email and line stats.
        """
        repo = self.client.get_repo(repo_id, lazy=True)
        pr = repo.get_pull(merge_request_id)
        result: list[MergeRequestCommit] = []
        for commit in pr.get_commits():
            author = commit.commit.author if commit.commit else None
            try:
                stats = commit.stats
                lines_added = stats.additions if stats else 0
                lines_removed = stats.deletions if stats else 0
            except GithubException:
                logger.warning(
                    "Failed to fetch stats for commit %s in %s#%d, skipping", commit.sha, repo_id, merge_request_id
                )
                lines_added = 0
                lines_removed = 0
            result.append(
                MergeRequestCommit(
                    sha=commit.sha,
                    author_email=(author.email if author else "") or "",
                    lines_added=lines_added,
                    lines_removed=lines_removed,
                )
            )
        return result

    def get_bot_commit_email(self) -> str:
        """
        Return the email address DAIV uses when authoring commits on GitHub.
        """
        bot_login = f"{self.client_installation.app_slug}[bot]"
        bot_user_id = self.current_user.id
        return f"{bot_user_id}+{bot_login}@users.noreply.github.com"

    def get_merge_request(self, repo_id: str, merge_request_id: int) -> MergeRequest:
        """
        Get a pull request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.

        Returns:
            The pull request.
        """
        repo = self.client.get_repo(repo_id, lazy=True)
        mr = repo.get_pull(merge_request_id)
        return MergeRequest(
            repo_id=repo_id,
            merge_request_id=merge_request_id,
            source_branch=mr.head.ref,
            target_branch=mr.base.ref,
            title=mr.title,
            description=mr.body or "",
            labels=[label.name for label in mr.labels],
            web_url=mr.html_url,
            sha=mr.head.sha,
            author=User(id=mr.user.id, username=mr.user.login, name=mr.user.name),
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
        pr = self.client.get_repo(repo_id, lazy=True).get_pull(merge_request_id)

        comment = None
        try:
            comment = pr.get_issue_comment(int(comment_id))
        except UnknownObjectException:
            comment = pr.get_review_comment(int(comment_id))

        if comment is None:
            return Discussion(id=str(comment_id), notes=[])

        return Discussion(id=str(comment_id), notes=self._serialize_comments([comment], from_merge_request=True))

    def create_merge_request_note_emoji(self, repo_id: str, merge_request_id: int, emoji: Emoji, note_id: int):
        """
        Create an emoji on a note of a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            emoji: The emoji name.
            note_id: The note ID.
        """
        if not (emoji_reaction := EMOJI_MAP.get(emoji)):
            raise ValueError(f"Unsupported emoji: {emoji}")

        pr = self.client.get_repo(repo_id, lazy=True).get_pull(merge_request_id)
        try:
            pr.get_review_comment(note_id).create_reaction(emoji_reaction)
        except UnknownObjectException:
            pr.get_issue_comment(note_id).create_reaction(emoji_reaction)

    def mark_merge_request_comment_as_resolved(self, repo_id: str, merge_request_id: int, discussion_id: str):
        """
        Mark a review as resolved.
        """
        _, result = self.client.requester.graphql_named_mutation(
            "resolveReviewThread", {"threadId": discussion_id}, "thread { id isResolved resolvedBy { login } }"
        )

        if result["thread"]["isResolved"]:
            return

    # User
    @cached_property
    def current_user(self) -> User:
        """
        Get the current user.
        """
        # GitHub name the bot with the app slug and [bot] suffix.
        # Maybe there's a better way to get the bot user, but this is the only way I found so far.

        user = self.client.get_user(f"{self.client_installation.app_slug}[bot]")
        return User(id=user.id, username=self.client_installation.app_slug, name=user.name)

    def _serialize_comments(
        self, comments: list[IssueComment | PullRequestComment], from_merge_request: bool = False
    ) -> list[Note]:
        """
        Get the notes of an issue or a merge request.
        """
        notes = []

        for note in comments:
            if isinstance(note, IssueComment):
                note_type = NoteType.DISCUSSION_NOTE
            elif isinstance(note, PullRequestComment):
                note_type = NoteType.DIFF_NOTE

            position = None
            if isinstance(note, PullRequestComment):
                position = NotePosition(
                    head_sha=note.commit_id,
                    old_path=note.path,
                    new_path=note.path,
                    position_type=NotePositionType.TEXT if note.subject_type == "line" else NotePositionType.FILE,
                    old_line=note.start_line,
                    new_line=note.line,
                    line_range=NotePositionLineRange(
                        start=NoteDiffPosition(
                            type=NoteDiffPositionType.NEW
                            if (note.start_side or note.side) == "RIGHT"
                            else NoteDiffPositionType.OLD,
                            old_line=note.start_line,
                            new_line=note.line,
                        ),
                        end=NoteDiffPosition(
                            type=NoteDiffPositionType.NEW
                            if (note.side or note.start_side) == "RIGHT"
                            else NoteDiffPositionType.OLD,
                            old_line=note.start_line,
                            new_line=note.line,
                        ),
                    ),
                )

            notes.append(
                Note(
                    id=note.id,
                    body=note.body,
                    type=note_type,
                    noteable_type=NoteableType.ISSUE if not from_merge_request else NoteableType.MERGE_REQUEST,
                    system=False,
                    resolvable=False,
                    resolved=False,
                    author=User(id=note.user.id, username=note.user.login, name=note.user.name),
                    position=position,
                )
            )
        return notes
