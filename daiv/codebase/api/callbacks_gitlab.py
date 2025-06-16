import logging
from functools import cached_property
from typing import Any, Literal

from asgiref.sync import sync_to_async

from codebase.api.callbacks import BaseCallback
from codebase.api.models import (
    Issue,
    IssueAction,
    MergeRequest,
    Note,
    NoteableType,
    NoteAction,
    Pipeline,
    PipelineBuild,
    Project,
    User,
)
from codebase.base import MergeRequest as BaseMergeRequest
from codebase.clients import RepoClient
from codebase.tasks import address_issue_task, address_review_task, fix_pipeline_job_task, update_index_repository
from codebase.utils import discussion_has_daiv_mentions, note_mentions_daiv
from core.config import RepositoryConfig

ISSUE_CHANGE_FIELDS = {"title", "description", "labels", "state_id"}
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
            # Only accept if there are changes in the title, description, labels or state of the issue.
            and bool(self.changes.keys() & ISSUE_CHANGE_FIELDS)
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

        if self.object_attributes.noteable_type == NoteableType.MERGE_REQUEST:
            if (
                not self._repo_config.features.auto_address_review_enabled
                or self.object_attributes.system
                or self.object_attributes.action != NoteAction.CREATE
                or not self.merge_request
                or self.merge_request.work_in_progress
                or self.merge_request.state != "opened"
                or self.user.id == client.current_user.id
            ):
                return False

            # Shortcut to avoid fetching the discussion if the note mentions DAIV.
            if note_mentions_daiv(self.object_attributes.note, client.current_user):
                return True

            # Fetch the discussion to check if it has any notes mentioning DAIV.
            discussion = client.get_merge_request_discussion(
                self.project.path_with_namespace, self.merge_request.iid, self.object_attributes.discussion_id
            )
            return discussion_has_daiv_mentions(discussion, client.current_user)

        elif self.object_attributes.noteable_type == NoteableType.ISSUE:
            return bool(
                self._repo_config.features.auto_address_issues_enabled
                and not self.object_attributes.system
                and self.object_attributes.action == NoteAction.CREATE
                and self.user.id != client.current_user.id
                and self.issue
                and self.issue.is_daiv()
                and self.issue.state == "opened"
            )

        return False

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
        return self.ref.endswith(self.project.default_branch) or any(mr.is_daiv() for mr in self.related_merge_requests)

    async def process_callback(self):
        """
        Process the push webhook to update the codebase index and invalidate the cache for the
        repository configurations.
        """
        if self.project.default_branch and self.ref.endswith(self.project.default_branch):
            # Invalidate the cache for the repository configurations, they could have changed.
            RepositoryConfig.invalidate_cache(self.project.path_with_namespace)
            await sync_to_async(
                update_index_repository.si(
                    repo_id=self.project.path_with_namespace, ref=self.project.default_branch
                ).delay
            )()

        for merge_request in self.related_merge_requests:
            if merge_request.is_daiv():
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


class PipelineStatusCallback(BaseCallback):
    """
    Gitlab Pipeline Status Webhook
    """

    object_kind: Literal["pipeline"]
    project: Project
    merge_request: MergeRequest | None = None
    object_attributes: Pipeline
    builds: list[PipelineBuild]

    def model_post_init(self, __context: Any):
        self._repo_config = RepositoryConfig.get_config(self.project.path_with_namespace)

    def accept_callback(self) -> bool:
        """
        Accept callback if the pipeline failed and has a failed build to fix.
        """
        return (
            self._repo_config.features.autofix_pipeline_enabled
            and self.object_attributes.status == "failed"
            and self._first_failed_build is not None
            and self._merge_request is not None
            and self._merge_request.is_daiv()
        )

    async def process_callback(self):
        """
        Trigger the task to fix the pipeline failed build.

        Only one build is fixed at a time to avoid two or more fixes being applied simultaneously to the same files,
        which could lead to conflicts or a job being fixed with outdated code.
        """
        if self.merge_request is not None and self._first_failed_build is not None:
            await sync_to_async(
                fix_pipeline_job_task.si(
                    repo_id=self.project.path_with_namespace,
                    ref=self.merge_request.source_branch,
                    merge_request_id=self.merge_request.iid,
                    job_id=self._first_failed_build.id,
                    job_name=self._first_failed_build.name,
                ).delay
            )()

    @cached_property
    def _merge_request(self) -> BaseMergeRequest | None:
        """
        Get the merge request related to the pipeline to obtain associated labels and infer if is a DAIV MR.
        """
        client = RepoClient.create_instance()
        if self.merge_request is not None:
            return client.get_merge_request(self.project.path_with_namespace, self.merge_request.iid)
        return None

    @cached_property
    def _first_failed_build(self) -> PipelineBuild | None:
        """
        Get the first failed build of the pipeline.
        """
        return next(
            (
                build
                for build in self.builds
                if build.status == "failed"
                and not build.manual
                and not build.allow_failure
                and build.failure_reason == "script_failure"
            ),
            None,
        )
