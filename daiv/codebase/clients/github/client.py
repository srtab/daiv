from __future__ import annotations

import logging
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZipFile

import httpx
from asgiref.sync import async_to_sync
from github import Auth, Consts, Github, GithubIntegration, InputGitTreeElement, Installation, UnknownObjectException
from github import Repository as GithubRepository
from github.GithubException import GithubException
from github.IssueComment import IssueComment
from github.PullRequestComment import PullRequestComment
from unidiff import PatchSet

from codebase.base import (
    ClientType,
    Discussion,
    FileChange,
    FileChangeAction,
    Issue,
    Job,
    MergeRequest,
    Note,
    NoteableType,
    NoteDiffPosition,
    NoteDiffPositionType,
    NotePosition,
    NotePositionLineRange,
    NotePositionType,
    NoteType,
    Pipeline,
    Repository,
    User,
)
from codebase.clients import RepoClient
from codebase.clients.base import Emoji
from codebase.conf import settings
from core.utils import async_download_url

if TYPE_CHECKING:
    from collections.abc import Iterator


logger = logging.getLogger("daiv.clients")

EMOJI_MAP = {Emoji.THUMBSUP: "+1"}


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

    def create_issue_note_emoji(self, repo_id: str, issue_id: int, emoji: Emoji, note_id: str):
        """
        Create an emoji in a note of an issue.
        """
        if not (emoji_reaction := EMOJI_MAP.get(emoji)):
            raise ValueError(f"Unsupported emoji: {emoji}")

        self.client.get_repo(repo_id, lazy=True).get_issue(issue_id).get_comment(note_id).create_reaction(
            emoji_reaction
        )

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
        # GitHub doesn't have discussions like GitLab. This is a workaround to get the notes of an issue.
        return Discussion(id=comment_id, notes=self._serialize_comments(issue.get_comments()))

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
            reply_to_id: The ID of the comment to reply to. This is not supported for GitHub.
        """
        self.client.get_repo(repo_id, lazy=True).get_issue(issue_id).get_comment(comment_id).edit(body)

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
            description=mr.body or "",
            labels=[label.name for label in mr.labels],
            sha=mr.head.sha,
        )

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

    def mark_merge_request_comment_as_resolved(self, repo_id: str, merge_request_id: int, discussion_id: str):
        """
        Mark a review as resolved.
        """
        _, result = self.client.requester.graphql_named_mutation(
            "resolveReviewThread", {"threadId": discussion_id}, "thread { id isResolved resolvedBy { login } }"
        )

        if result["thread"]["isResolved"]:
            return

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

    def create_merge_request_note_emoji(self, repo_id: str, merge_request_id: int, emoji: Emoji, note_id: str):
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

    def get_merge_request_diff(self, repo_id: str, merge_request_id: int) -> PatchSet:
        """
        Get the diff of a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request ID.

        Returns:
            The diff patch set.
        """
        pr = self.client.get_repo(repo_id, lazy=True).get_pull(merge_request_id)
        headers, data = self.client.requester.requestJsonAndCheck("GET", pr.diff_url, follow_302_redirect=True)
        return PatchSet.from_string(data["data"])

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
            comment = pr.get_issue_comment(comment_id)
        except UnknownObjectException:
            comment = pr.get_review_comment(comment_id)

        if comment is None:
            return Discussion(id=comment_id, notes=[])

        return Discussion(id=comment_id, notes=self._serialize_comments([comment], from_merge_request=True))

    def get_merge_request_review_comments(self, repo_id: str, merge_request_id: int) -> list[Discussion]:
        """
        Get the review comments left on the merge request diff.
        """
        pr = self.client.get_repo(repo_id, lazy=True).get_pull(merge_request_id)
        # For review comments, we need to group them by comment ID because GitHub supports nested comments.
        unresolved_comment_ids, comment_to_thread_id = self._unresolved_comment_ids(repo_id, merge_request_id)

        comments = defaultdict(list)
        thread_ids = {}  # Map comment_id to thread_id

        for comment in pr.get_review_comments():
            if comment.id not in unresolved_comment_ids:
                # Skip resolved comments
                continue

            comment_id = comment.in_reply_to_id or comment.id
            comments[comment_id] += self._serialize_comments([comment], from_merge_request=True)

            # Store the thread ID for this comment group (use the first comment's thread ID we find)
            if comment_id not in thread_ids and comment.id in comment_to_thread_id:
                thread_ids[comment_id] = comment_to_thread_id[comment.id]

        return [
            Discussion(
                id=str(comment_id),
                notes=notes,
                is_thread=True,
                is_resolvable=True,
                resolve_id=thread_ids.get(comment_id),
            )
            for comment_id, notes in comments.items()
        ]

    def get_merge_request_comments(self, repo_id: str, merge_request_id: int) -> list[Discussion]:
        """
        Get the comments done directly on a merge request (not in a review thread).
        """
        pr = self.client.get_repo(repo_id, lazy=True).get_pull(merge_request_id)
        return [
            Discussion(id=str(comment.id), notes=self._serialize_comments([comment], from_merge_request=True))
            for comment in pr.get_issue_comments()
        ]

    def _unresolved_comment_ids(self, repo_id: str, merge_request_id: int) -> tuple[set[int], dict[int, str]]:
        """
        Get the threads resolution of a merge request.

        Returns:
            A tuple of (unresolved_comment_ids, comment_to_thread_id_map)
        """
        query = """
            query($owner: String!, $repo: String!, $pullRequest: Int!) {
                repository(owner: $owner, name: $repo) {
                    pullRequest(number: $pullRequest) {
                        reviewThreads(first: 100) {
                            nodes {
                                id
                                isResolved
                                comments(first: 100) {
                                    nodes {
                                        databaseId
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
            query, {"owner": repo.owner.login, "repo": repo.name, "pullRequest": merge_request_id}
        )
        unresolved_comment_ids = set()
        comment_to_thread_id = {}

        for thread in result["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
            thread_id = thread["id"]

            if thread["isResolved"]:
                continue

            for comment in thread["comments"]["nodes"]:
                comment_db_id = comment["databaseId"]
                unresolved_comment_ids.add(comment_db_id)
                # Map each comment to its thread ID
                comment_to_thread_id[comment_db_id] = thread_id

        return unresolved_comment_ids, comment_to_thread_id

    def get_merge_request_latest_pipelines(self, repo_id: str, merge_request_id: int) -> list[Pipeline]:
        """
        Get the latest pipeline (workflow run) of a pull request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The pull request number.

        Returns:
            List of Pipeline objects with workflow run information, or empty list if no runs found.
        """
        repo = self.client.get_repo(repo_id, lazy=True)
        pr = repo.get_pull(merge_request_id)

        pipelines = []
        for run in repo.get_workflow_runs(head_sha=pr.head.sha, event="pull_request"):
            jobs_list = []
            for job in run.jobs():
                if job.status == "completed":
                    # Map GitHub conclusion to our status
                    conclusion_mapping = {
                        "success": "success",
                        "failure": "failed",
                        "cancelled": "canceled",
                        "skipped": "skipped",
                    }
                    status = conclusion_mapping.get(job.conclusion, "success")
                else:
                    # Map GitHub status to our status
                    status_mapping = {"queued": "pending", "in_progress": "running", "waiting": "pending"}
                    status = status_mapping.get(job.status, "pending")

                jobs_list.append(
                    Job(
                        id=job.id,
                        name=job.name,
                        status=status,
                        stage=job.name,  # GitHub doesn't have stages, use job name
                        allow_failure=False,  # GitHub doesn't have this concept natively
                        failure_reason=job.conclusion if job.conclusion in ["failure", "cancelled"] else None,
                    )
                )

            pipelines.append(
                Pipeline(
                    id=run.id,
                    iid=run.run_number,
                    status=run.status if run.status != "completed" else (run.conclusion or "success"),
                    sha=run.head_sha,
                    web_url=run.html_url,
                    jobs=jobs_list,
                )
            )
        return pipelines

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

    def get_job(self, repo_id: str, job_id: int):
        """
        Get a GitHub Actions job by its ID.

        Args:
            repo_id: The repository ID.
            job_id: The job ID.

        Returns:
            Job object with job details.
        """
        # Get the workflow job using the API
        # We need to use the REST API directly as PyGithub doesn't have direct job access
        _headers, data = self.client.requester.requestJsonAndCheck("GET", f"/repos/{repo_id}/actions/jobs/{job_id}")

        # Map GitHub Actions job status/conclusion to our status format
        if data["status"] == "completed":
            conclusion_mapping = {
                "success": "success",
                "failure": "failed",
                "cancelled": "canceled",
                "skipped": "skipped",
            }
            status = conclusion_mapping.get(data["conclusion"], "success")
        else:
            status_mapping = {"queued": "pending", "in_progress": "running", "waiting": "pending"}
            status = status_mapping.get(data["status"], "pending")

        return Job(
            id=data["id"],
            name=data["name"],
            status=status,
            stage=data["name"],  # GitHub doesn't have stages, use job name
            allow_failure=False,  # GitHub doesn't have this concept natively
            failure_reason=data["conclusion"] if data["conclusion"] in ["failure", "cancelled"] else None,
        )

    @async_to_sync
    async def job_log_trace(self, repo_id: str, job_id: int) -> str:
        """
        Get the log trace of a GitHub Actions job.

        Args:
            repo_id: The repository ID.
            job_id: The job ID.

        Returns:
            The log trace of the job as a string.
        """
        try:
            # Use the requester to make a direct API call for job logs
            # The logs endpoint returns a 302 redirect to the actual log content
            headers, _ = self.client.requester.requestBlobAndCheck(
                "GET", f"/repos/{repo_id}/actions/jobs/{job_id}/logs"
            )
        except GithubException:
            return None

        # GitHub responds with a 302 Location -> temporary plain-text log URL
        if "location" in headers:
            response = await async_download_url(headers["location"])
            return response.decode("utf-8")

        return None

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
