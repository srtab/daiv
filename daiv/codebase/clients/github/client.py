from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZipFile

import httpx
from github import Auth, Consts, Github, GithubIntegration, InputGitTreeElement, Installation, UnknownObjectException
from github import Issue as GithubIssue
from github import PullRequest as GithubPullRequest
from github import Repository as GithubRepository
from github.GithubException import GithubException

from codebase.base import (
    ClientType,
    Discussion,
    FileChange,
    FileChangeAction,
    Issue,
    MergeRequest,
    Note,
    NoteableType,
    NoteType,
    Repository,
    User,
)
from codebase.clients import RepoClient
from codebase.conf import settings

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger("daiv.clients")


class GitHubClient(RepoClient):
    """
    GitHub client to interact with GitHub repositories.
    """

    client: Github
    client_installation: Installation.Installation
    client_slug = ClientType.GITHUB

    def __init__(self, private_key: str, app_id: int, installation_id: int, url: str | None = None):
        if url is None:
            url = Consts.DEFAULT_BASE_URL

        integration = GithubIntegration(
            auth=Auth.AppAuth(app_id, private_key), base_url=url, user_agent=settings.CLIENT_USER_AGENT, per_page=100
        )
        self.client_installation = integration.get_app_installation(installation_id)
        self.client = self.client_installation.get_github_for_installation()

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
            default_branch=repo.default_branch,
            client=self.client_slug,
            topics=repo.topics,
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
        if search:
            raise NotImplementedError("Search is not supported for GitHub client.")

        return [
            Repository(
                pk=repo.id,
                slug=repo.full_name,
                name=repo.name,
                default_branch=repo.default_branch,
                client=self.client_slug,
                topics=repo.topics,
            )
            for repo in self.client_installation.get_repos()
            if topics is None or any(topic in repo.topics for topic in topics)
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
        repo = self.client.get_repo(repo_id, lazy=True)
        return repo.get_contents(file_path, ref=ref).decoded_content.decode()

    def repository_branch_exists(self, repo_id: str, branch: str) -> bool:
        """
        Check if a branch exists in a repository.
        """
        repo = self.client.get_repo(repo_id, lazy=True)
        try:
            repo.get_branch(branch)
            return True
        except UnknownObjectException:
            return False

    def set_repository_webhooks(
        self,
        repo_id: str,
        url: str,
        push_events_branch_filter: str | None = None,
        enable_ssl_verification: bool = True,
        secret_token: str | None = None,
    ):
        """
        Set webhooks for a repository.
        """
        events = ["push", "issues", "pull_request_review", "issue_comment"]
        config = {
            "url": url,
            "content_type": "json",
            "secret": secret_token,
            "insecure_ssl": not enable_ssl_verification,
        }
        repo = self.client.get_repo(repo_id, lazy=True)

        for hook in repo.get_hooks():
            if hook.url == url:
                hook.edit("web", config, events, active=True)
                return True

        repo.create_hook("web", config, events, active=True)
        return True

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

    def create_issue_comment(self, repo_id: str, issue_id: int, body: str) -> str | None:
        """
        Comment on an issue.

        Returns:
            The comment ID.
        """
        return self.client.get_repo(repo_id, lazy=True).get_issue(issue_id).create_comment(body).id

    def update_issue_comment(self, repo_id: str, issue_id: int, comment_id: int, body: str):
        """
        Update a comment on an issue.
        """
        self.client.get_repo(repo_id, lazy=True).get_issue(issue_id).get_comment(comment_id).edit(body)

    def create_issue_note_emoji(self, repo_id: str, issue_id: int, emoji: str, note_id: str):
        """
        Create an emoji in a note of an issue.
        """
        self.client.get_repo(repo_id, lazy=True).get_issue(issue_id).get_comment(note_id).create_reaction(emoji)

    def get_issue_discussion(
        self, repo_id: str, issue_id: int, discussion_id: str, only_resolvable: bool = True
    ) -> Discussion:
        """
        Get a discussion from an issue.

        For GitHub, there's no distinction between discussions and notes.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            discussion_id: The discussion ID. This is not used for GitHub.
            only_resolvable: Whether to only return resolvable notes. This is not used for GitHub.

        Returns:
            The discussion object.
        """
        issue = self.client.get_repo(repo_id, lazy=True).get_issue(issue_id)
        # GitHub doesn't have discussions like GitLab. This is a workaround to get the notes of an issue.
        return Discussion(id=discussion_id, notes=self._serialize_comments(issue.get_comments()), is_reply=False)

    def create_issue_discussion_note(
        self, repo_id: str, issue_id: int, body: str, discussion_id: str | None = None
    ) -> str | None:
        """
        Create a comment on an issue.

        For GitHub, there's no distinction between discussions and notes. This method creates a comment on the issue.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            body: The comment body.
            discussion_id: The discussion ID. This is not used for GitHub.
        """
        # GitHub doesn't have discussions like GitLab. This is a workaround to create a comment on the issue.
        return self.create_issue_comment(repo_id, issue_id, body)

    def update_issue_discussion_note(self, repo_id: str, issue_id: int, discussion_id: str, note_id: str, body: str):
        """
        Update a comment on an issue.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            discussion_id: The discussion ID. This is not used for GitHub.
            note_id: The note ID.
            body: The comment body.
        """
        self.update_issue_comment(repo_id, issue_id, note_id, body)

    @cached_property
    def current_user(self) -> User:
        """
        Get the current user.
        """
        # GitHub name the bot with the app slug and [bot] suffix.
        # Maybe there's a better way to get the bot user, but this is the only way I found so far.

        user = self.client.get_user(f"{self.client_installation.app_slug}[bot]")
        return User(id=user.id, username=self.client_installation.app_slug, name=user.name)

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
            description=mr.body,
            labels=[label.name for label in mr.labels],
            sha=mr.head.sha,
        )

    def comment_merge_request(self, repo_id: str, merge_request_id: int, body: str) -> str | None:
        """
        Comment on a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            body: The comment body.
        """
        return self.client.get_repo(repo_id, lazy=True).get_pull(merge_request_id).create_comment(body).id

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
            target_branch: The target branch.
            commit_message: The commit message.
            file_changes: The list of file changes.
            start_branch: The start branch to base the commit on. If None, uses target_branch.
            override_commits: Whether to override existing commits (force-push behavior).
        """
        repo = self.client.get_repo(repo_id, lazy=True)

        base_branch = start_branch or target_branch

        try:
            base_branch_obj = repo.get_branch(base_branch)
            head_sha = base_branch_obj.commit.sha
        except UnknownObjectException as err:
            raise ValueError(f"Base branch '{base_branch}' does not exist") from err

        try:
            target_ref = repo.get_git_ref(f"heads/{target_branch}")
        except UnknownObjectException:
            target_ref = repo.create_git_ref(ref=f"refs/heads/{target_branch}", sha=head_sha)

        # Create the new tree and commit
        elements = self._create_git_tree_element(repo, file_changes, head_sha)
        base_tree = repo.get_git_tree(sha=head_sha)
        tree = repo.create_git_tree(elements, base_tree)
        parent = repo.get_git_commit(sha=head_sha)
        new_commit = repo.create_git_commit(commit_message, tree, [parent])

        # Update the target branch reference
        target_ref.edit(sha=new_commit.sha, force=override_commits)

    def _create_git_tree_element(
        self, repo: GithubRepository.Repository, file_changes: list[FileChange], head_sha: str
    ) -> list[InputGitTreeElement]:
        elements = []

        for file_change in file_changes:
            if file_change.action in [FileChangeAction.CREATE, FileChangeAction.UPDATE]:
                blob = repo.create_git_blob(file_change.content, "utf-8")
                elements.append(
                    InputGitTreeElement(path=file_change.file_path, mode="100644", type="blob", sha=blob.sha)
                )

            elif file_change.action == FileChangeAction.DELETE:
                elements.append(
                    InputGitTreeElement(
                        path=file_change.file_path,
                        mode="100644",
                        type="blob",
                        sha=None,  # This signals deletion
                    )
                )

            elif file_change.action == FileChangeAction.MOVE:
                # For move operations, we need to handle both the old and new paths
                if file_change.previous_path:
                    # Delete the old file
                    elements.append(
                        InputGitTreeElement(
                            path=file_change.previous_path,
                            mode="100644",
                            type="blob",
                            sha=None,  # This signals deletion
                        )
                    )

                # Create the new file (with content if provided, otherwise preserve existing content)
                if not file_change.content:
                    blob = repo.create_git_blob(file_change.content, "utf-8")
                else:
                    # If no content provided, try to get existing content from previous path
                    try:
                        existing_file = repo.get_contents(file_change.previous_path, ref=head_sha)
                        blob = repo.create_git_blob(existing_file.decoded_content.decode(), "utf-8")
                    except UnknownObjectException as err:
                        raise ValueError(f"Cannot move file '{file_change.previous_path}': file not found") from err

                elements.append(
                    InputGitTreeElement(path=file_change.file_path, mode="100644", type="blob", sha=blob.sha)
                )
            else:
                raise ValueError(f"Unsupported file change action: {file_change.action}")

        return elements

    def create_merge_request_discussion_note(
        self,
        repo_id: str,
        merge_request_id: int,
        body: str,
        discussion_id: str | None = None,
        mark_as_resolved: bool = False,
    ):
        """
        Create a comment on a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            body: The comment body.
            discussion_id: The discussion ID.
        """
        return self.comment_merge_request(repo_id, merge_request_id, body)

    def update_merge_request_discussion_note(
        self, repo_id: str, merge_request_id: int, discussion_id: str, note_id: str, body: str
    ):
        """
        Update a discussion in a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            discussion_id: The discussion ID. This is not used for GitHub.
            note_id: The note ID.
            body: The note body.
        """
        self.client.get_repo(repo_id, lazy=True).get_pull(merge_request_id).get_comment(note_id).edit(body)

    def create_merge_request_note_emoji(self, repo_id: str, merge_request_id: int, emoji: str, note_id: str):
        """
        Create an emoji on a note of a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.
            emoji: The emoji name.
            note_id: The note ID.
        """
        self.client.get_repo(repo_id, lazy=True).get_pull(merge_request_id).get_comment(note_id).create_reaction(emoji)

    def get_issue_related_merge_requests(
        self, repo_id: str, issue_id: int, assignee_id: int | None = None, label: str | None = None
    ) -> list[MergeRequest]:
        """
        Get the related merge requests of an issue.
        """
        query = """
            query($owner: String!, $repo: String!, $issue: Int!) {
                repository(owner: $owner, name: $repo) {
                    issue(number: $issue) {
                        number
                        timelineItems(itemTypes: [CONNECTED_EVENT], first: 20) {
                            nodes {
                                ... on ConnectedEvent {
                                    subject {
                                        ... on PullRequest {
                                            number
                                            state
                                            title
                                            body
                                            labels(first: 10) {
                                                nodes {
                                                    name
                                                }
                                            }
                                            headRefName
                                            baseRefName
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        """
        repo = self.client.get_repo(repo_id)

        _, result = self.client.requester.graphql_query(
            query, {"owner": repo.owner.login, "repo": repo.name, "issue": issue_id}
        )

        linked_prs = []

        for item in result["data"]["repository"]["issue"]["timelineItems"]["nodes"]:
            if (node := item.get("subject")) and (
                label is None or (labels := node.get("labels")) and any(mr_label.name == label for mr_label in labels)
            ):
                linked_prs.append(
                    MergeRequest(
                        repo_id=repo_id,
                        merge_request_id=node["number"],
                        source_branch=node["headRefName"],
                        target_branch=node["baseRefName"],
                        title=node["title"],
                        description=node["body"],
                        labels=[mr_label.name for mr_label in labels] if labels else [],
                        state=node["state"],
                    )
                )

        return linked_prs

    def get_merge_request_diff(self, repo_id: str, merge_request_id: int):
        return self.client.get_repo(repo_id, lazy=True).get_pull(merge_request_id)

    def get_merge_request_discussion(self, repo_id: str, merge_request_id: int, discussion_id: str):
        pr = self.client.get_repo(repo_id, lazy=True).get_pull(merge_request_id)
        return Discussion(id=discussion_id, notes=self._serialize_comments(pr.get_comments()))

    def get_merge_request_discussions(self, repo_id: str, merge_request_id: int):
        raise NotImplementedError()

    def get_merge_request_latest_pipeline(self, repo_id: str, merge_request_id: int):
        raise NotImplementedError()

    def get_project_uploaded_file(self, repo_id: str, file_path: str):
        raise NotImplementedError()

    def get_repository_file_link(self, repo_id: str, file_path: str, ref: str):
        raise NotImplementedError()

    def job_log_trace(self, repo_id: str, job_id: int):
        raise NotImplementedError()

    @contextmanager
    def load_repo(self, repository: Repository, sha: str) -> Iterator[Path]:
        client_repo = self.client.get_repo(repository.slug, lazy=True)

        safe_sha = sha.replace("/", "_").replace(" ", "-")

        tmpdir = tempfile.TemporaryDirectory(prefix=f"{repository.pk}-{safe_sha}-repo")
        logger.debug("Loading repository to %s", tmpdir)

        archive_url = client_repo.get_archive_link("zipball", ref=sha)

        try:
            with (
                tempfile.NamedTemporaryFile(
                    prefix=f"{repository.pk}-{safe_sha}-archive", suffix=".zip"
                ) as repo_archive,
                httpx.stream(
                    "GET", archive_url, timeout=10.0, headers={"User-Agent": settings.CLIENT_USER_AGENT}
                ) as response,
            ):
                response.raise_for_status()

                for line in response.iter_bytes():
                    repo_archive.write(line)

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
        Update or create a merge request.

        Args:
            repo_id: The repository ID.
            source_branch: The source branch.
            target_branch: The target branch.
            title: The title.
            description: The description.
            labels: The labels.
            assignee_id: The assignee ID.

        Returns:
            The merge request ID.
        """
        repo = self.client.get_repo(repo_id, lazy=True)

        try:
            pr = repo.create_pull(base=target_branch, head=source_branch, title=title, body=description)
        except GithubException as e:
            if e.status != 409:
                raise e

            prs = repo.get_pulls(base=target_branch, head=source_branch, state="open")

            if not prs:
                raise e

            pr = prs[0]
            pr.edit(title=title, body=description)

        if labels is not None:
            pr.add_to_labels(*labels)

        if assignee_id and not any(assignee.id == assignee_id for assignee in pr.assignees):
            pr.add_to_assignees(assignee_id)

        return pr.number

    def _serialize_comments(
        self, comments: list[GithubIssue.IssueComment | GithubPullRequest.PullRequestComment]
    ) -> list[Note]:
        """
        Get the notes of an issue or a merge request.
        """
        return [
            Note(
                id=note.id,
                body=note.body,
                type=NoteType.NOTE,
                noteable_type=NoteableType.ISSUE
                if isinstance(note, GithubIssue.IssueComment)
                else NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
                author=User(id=note.user.id, username=note.user.login, name=note.user.name),
            )
            for note in comments
        ]
