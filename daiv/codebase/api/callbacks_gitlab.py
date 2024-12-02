import logging
import re
from functools import cached_property
from typing import Any, Literal

from asgiref.sync import sync_to_async

from codebase.api.callbacks import BaseCallback
from codebase.api.models import Issue, IssueAction, MergeRequest, Note, NoteableType, NoteAction, Project, User
from codebase.base import MergeRequest as BaseMergeRequest
from codebase.clients import RepoClient
from codebase.tasks import address_issue_task, address_review_task, fix_pipeline_job_task, update_index_repository
from core.config import RepositoryConfig
from core.utils import generate_uuid

PIPELINE_JOB_REF_SUFFIX = "refs/merge-requests/"

logger = logging.getLogger("daiv.webhooks")


class IssueCallback(BaseCallback):
    """
    Gitlab Issue Webhook
    """

    object_kind: Literal["issue", "work_item"]
    project: Project
    user: User
    object_attributes: Issue
    changes: dict

    def accept_callback(self) -> bool:
        return (
            RepositoryConfig.get_config(self.project.path_with_namespace).features.auto_address_issues_enabled
            and self.object_attributes.action in [IssueAction.OPEN, IssueAction.UPDATE]
            # Only accept if there are changes in the title or description of the issue.
            and (self.changes and "title" in self.changes or "description" in self.changes or "labels" in self.changes)
            # Only accept if the issue is a DAIV issue.
            and self.object_attributes.is_daiv()
            # When work_item is created without the parent issue, the object_kind=issue.
            # We need to check the type too to avoid processing work_items as issues.
            and self.object_kind == "issue"
            and self.object_attributes.type == "Issue"
            and self.object_attributes.state == "opened"
        )

    async def process_callback(self):
        await sync_to_async(
            address_issue_task.si(
                repo_id=self.project.path_with_namespace,
                issue_iid=self.object_attributes.iid,
                should_reset_plan=self.should_reset_plan(),
            ).delay
        )()

    def should_reset_plan(self) -> bool:
        """
        Check if the plan should be reset.
        """
        return bool(
            self.object_attributes.action == IssueAction.UPDATE
            and self.changes
            and any(field_name in self.changes for field_name in ["title", "description"])
        )


class NoteCallback(BaseCallback):
    """
    Gitlab Note Webhook
    """

    object_kind: Literal["note"]
    project: Project
    user: User
    merge_request: MergeRequest | None = None
    issue: Issue | None = None
    object_attributes: Note

    def model_post_init(self, __context: Any):
        self._repo_config = RepositoryConfig.get_config(self.project.path_with_namespace)

    def accept_callback(self) -> bool:
        """
        Accept the webhook if the note is a review feedback for a merge request.
        """
        client = RepoClient.create_instance()
        return bool(
            (
                self._repo_config.features.auto_address_issues_enabled
                or self._repo_config.features.auto_address_review_enabled
            )
            and not self.object_attributes.system
            and self.object_attributes.action == NoteAction.CREATE
            and (
                (
                    self.object_attributes.noteable_type == NoteableType.MERGE_REQUEST
                    and self.merge_request
                    and not self.merge_request.work_in_progress
                    and self.merge_request.state == "opened"
                    and self.merge_request.is_daiv()
                )
                or (
                    self.object_attributes.noteable_type == NoteableType.ISSUE
                    and self.user.id != client.current_user.id
                    and self.issue
                    and self.issue.is_daiv()
                    and self.issue.state == "opened"
                )
            )
        )

    async def process_callback(self):
        """
        Trigger the task to address the review feedback or issue comment like the plan approval use case.

        GitLab Note Webhook is called multiple times, one per note/discussion.
        """
        if self._repo_config.features.auto_address_review_enabled and self.merge_request:
            await sync_to_async(
                address_review_task.si(
                    repo_id=self.project.path_with_namespace,
                    merge_request_id=self.merge_request.iid,
                    merge_request_source_branch=self.merge_request.source_branch,
                ).delay
            )()

        if self._repo_config.features.auto_address_issues_enabled and self.issue:
            await sync_to_async(
                address_issue_task.si(repo_id=self.project.path_with_namespace, issue_iid=self.issue.iid).delay
            )()


class PushCallback(BaseCallback):
    """
    Gitlab Push Webhook for automatically update the codebase index.
    """

    object_kind: Literal["push"]
    project: Project
    checkout_sha: str
    ref: str

    def accept_callback(self) -> bool:
        """
        Accept the webhook if the push is to the default branch or to any branch with MR created.
        """
        return self.ref.endswith(self.project.default_branch) or bool(self.related_merge_requests)

    async def process_callback(self):
        """
        Process the push webhook to update the codebase index and invalidate the cache for the
        repository configurations.
        """
        if self.ref.endswith(self.project.default_branch):
            # Invalidate the cache for the repository configurations, they could have changed.
            RepositoryConfig.invalidate_cache(self.project.path_with_namespace)
            await sync_to_async(
                update_index_repository.si(
                    repo_id=self.project.path_with_namespace, ref=self.project.default_branch
                ).delay
            )()

        for merge_request in self.related_merge_requests:
            await sync_to_async(
                update_index_repository.si(
                    repo_id=self.project.path_with_namespace, ref=merge_request.source_branch
                ).delay
            )()

    @cached_property
    def related_merge_requests(self) -> list[BaseMergeRequest]:
        """
        Get the merge requests related to the push.
        """
        client = RepoClient.create_instance()
        return client.get_commit_related_merge_requests(self.project.path_with_namespace, commit_sha=self.checkout_sha)


class PipelineJobCallback(BaseCallback):
    """
    Gitlab Pipeline Job Webhook
    """

    object_kind: Literal["build"]
    project: Project
    sha: str
    ref: str
    build_id: int
    build_name: str
    build_allow_failure: bool
    build_status: Literal[
        "created", "pending", "running", "failed", "success", "canceled", "skipped", "manual", "scheduled"
    ]
    build_failure_reason: str

    def model_post_init(self, __context: Any):
        self._repo_config = RepositoryConfig.get_config(self.project.path_with_namespace)

    def accept_callback(self) -> bool:
        """
        Accept the webhook if the pipeline job failed due to a script failure and there are related merge requests.
        """
        return (
            not self.build_allow_failure
            and self._repo_config.features.autofix_pipeline_enabled
            and self.build_status == "failed"
            # Only fix pipeline jobs that failed due to a script failure.
            and self.build_failure_reason == "script_failure"
            # Only fix pipeline jobs of the latest commit of the merge request.
            and self.merge_request is not None
            and self.merge_request.is_daiv()
            and self.merge_request.sha == self.sha
        )

    async def process_callback(self):
        """
        Trigger the task to fix the pipeline job.
        """
        if self.merge_request:
            await sync_to_async(
                fix_pipeline_job_task.si(
                    repo_id=self.project.path_with_namespace,
                    ref=self.merge_request.source_branch,
                    merge_request_id=self.merge_request.merge_request_id,
                    job_id=self.build_id,
                    job_name=self.build_name,
                    thread_id=generate_uuid(
                        f"{self.project.path_with_namespace}{self.merge_request.merge_request_id}{self.build_name}"
                    ),
                ).delay
            )()

    @cached_property
    def merge_request(self) -> BaseMergeRequest | None:
        """
        Get the merge request related to the job.
        """
        # The ref points to the source branch of a merge request.
        match = re.search(rf"{PIPELINE_JOB_REF_SUFFIX}(\d+)(?:/\w+)?$", self.ref)
        if match:
            client = RepoClient.create_instance()
            return client.get_merge_request(self.project.path_with_namespace, int(match.group(1)))
        return None
