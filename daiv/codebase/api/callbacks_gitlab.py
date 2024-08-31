import logging
import textwrap
from typing import TYPE_CHECKING, Literal

from django.core.cache import cache

from automation.agents.models import Usage
from automation.coders.change_describer.coder import ChangesDescriberCoder
from automation.coders.refactor.coder_simple import SimpleRefactorCoder
from automation.coders.refactor.prompts import RefactorPrompts
from codebase.api.callbacks import BaseCallback
from codebase.api.models import Issue, IssueAction, MergeRequest, Note, NoteableType, NoteAction, Project, User
from codebase.clients import RepoClient
from codebase.tasks import handle_mr_feedback, update_index_repository

if TYPE_CHECKING:
    from automation.coders.change_describer.models import ChangesDescriber
    from codebase.base import FileChange

logger = logging.getLogger(__name__)

LABEL_DAIV = "daiv"


class IssueCallback(BaseCallback):
    """
    Gitlab Issue Webhook
    """

    object_kind: Literal["issue", "work_item"]
    project: Project
    user: User
    object_attributes: Issue

    def accept_callback(self) -> bool:
        client = RepoClient.create_instance()
        return (
            self.object_attributes.action in [IssueAction.OPEN, IssueAction.UPDATE, IssueAction.REOPEN]
            and client.current_user.id == self.object_attributes.assignee_id
        )

    async def process_callback(self):
        client = RepoClient.create_instance()
        issue_notes = client.get_issue_notes(self.project.path_with_namespace, self.object_attributes.iid)
        if not next((note.body for note in issue_notes if note.author.id == client.current_user.id), None):
            client.comment_issue(
                self.project.path_with_namespace,
                self.object_attributes.iid,
                textwrap.dedent(
                    """\
                    Hello @{assignee}, I am a bot and I am here to help you refactor your codebase.

                    I will be processing this issue and creating a merge request with the changes
                    described on the description.

                    Please wait for a moment while I process the issue.
                    """
                ).format(assignee=self.user.username),
            )

        usage = Usage()

        changes: list[FileChange] = SimpleRefactorCoder(usage=usage).invoke(
            prompt=RefactorPrompts.format_task_prompt(self.object_attributes.description),
            source_repo_id=self.project.path_with_namespace,
            source_ref=self.project.default_branch,
        )

        if changes:
            changes_description: ChangesDescriber | None = ChangesDescriberCoder(usage).invoke(
                changes=[". ".join(file_change.commit_messages) for file_change in changes]
            )

            if changes_description is None:
                client.comment_issue(
                    self.project.path_with_namespace,
                    self.object_attributes.iid,
                    "There were an unexpected problem generating the changes description.",
                )
                return

            merge_requests = client.get_issue_related_merge_requests(
                self.project.path_with_namespace, self.object_attributes.iid, label=LABEL_DAIV
            )
            if len(merge_requests) > 1:
                client.comment_issue(
                    self.project.path_with_namespace,
                    self.object_attributes.iid,
                    textwrap.dedent(
                        """\
                        There are more than one merge requests related to this issue.
                        I am unable to proceed with the changes.
                        Please close the extra merge requests.
                        """
                    ),
                )
                return
            elif len(merge_requests) == 1:
                changes_description.branch = merge_requests[0].source_branch

            client.commit_changes(
                self.project.path_with_namespace,
                changes_description.branch,
                changes_description.commit_message,
                changes,
                start_branch=self.project.default_branch,
                override_commits=True,
            )
            merge_request_id = client.update_or_create_merge_request(
                repo_id=self.project.path_with_namespace,
                source_branch=changes_description.branch,
                target_branch=self.project.default_branch,
                labels=[LABEL_DAIV],
                title=changes_description.title,
                description=textwrap.dedent(
                    """\
                    👋 Hi there! This PR was automatically generated based on {source_repo_id}#{issue_id}

                    > {description}

                    ### 📣 Instructions for the reviewer which is you, yes **you**:
                    - **If these changes were incorrect, please close this PR and comment explaining why.**
                    - **If these changes were incomplete, please continue working on this PR then merge it.**
                    - **If you are feeling confident in my changes, please merge this PR.**

                    This will greatly help us improve the DAIV system. Thank you! 🙏

                    ### 🤓 Stats for the nerds:
                    Prompt tokens: **{prompt_tokens:,}** \\
                    Completion tokens: **{completion_tokens:,}** \\
                    Total tokens: **{total_tokens:,}** \\
                    Estimated cost: **${total_cost:.10f}**"""
                ).format(
                    description=changes_description.description,
                    source_repo_id=self.project.path_with_namespace,
                    issue_id=self.object_attributes.iid,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    total_cost=usage.cost,
                ),
            )
            client.comment_issue(
                self.project.path_with_namespace,
                self.object_attributes.iid,
                textwrap.dedent(
                    """\
                    This issue has been successfully processed.
                    I have created a merge request for you with the requested changes:
                    {source_repo_id}!{merge_request_id}.

                    Please review the changes and follow the instructions in the description of the merge request.

                    Thank you for using DAIV! 🚀
                    """
                ).format(source_repo_id=self.project.path_with_namespace, merge_request_id=merge_request_id),
            )


class NoteCallback(BaseCallback):
    """
    Gitlab Note Webhook
    """

    object_kind: Literal["note"]
    project: Project
    user: User
    merge_request: MergeRequest | None = None
    object_attributes: Note

    def accept_callback(self) -> bool:
        """
        Accept the webhook if the note is a review feedback for a merge request.
        """
        client = RepoClient.create_instance()
        return bool(
            self.object_attributes.noteable_type == NoteableType.MERGE_REQUEST
            and self.user.id != client.current_user.id
            and not self.object_attributes.system
            and self.object_attributes.action == NoteAction.CREATE
            and self.merge_request
            and not self.merge_request.work_in_progress
            and self.merge_request.state == "opened"
            and client.current_user.id == self.merge_request.assignee_id
        )

    async def process_callback(self):
        """
        Process the webhook by generating the changes and committing them to the source branch.

        GitLab Note Webhook is called multiple times, one per note/discussion.
        We need to prevent multiple webhook processing for the same merge request.
        """
        cache_key = f"{self.project.path_with_namespace}:{self.merge_request.iid}"
        with await cache.alock(f"{cache_key}::lock"):
            if await cache.aget(cache_key) is None:
                await cache.aset(cache_key, "launched", timeout=60 * 10)
                # handle_mr_feedback.si(
                handle_mr_feedback(
                    repo_id=self.project.path_with_namespace,
                    merge_request_id=self.merge_request.iid,
                    merge_request_source_branch=self.merge_request.source_branch,
                )
                # ).apply_async()
            else:
                logger.info(
                    "Merge request %s is already being processed. Skipping the webhook processing.",
                    self.merge_request.iid,
                )


class PushCallback(BaseCallback):
    """
    Gitlab Push Webhook to update the codebase index when a push is made to the default branch.
    """

    object_kind: Literal["push"]
    project: Project
    checkout_sha: str
    ref: str

    def accept_callback(self) -> bool:
        """
        Accept the webhook if the push is to the default branch.
        """
        return self.ref.endswith(self.project.default_branch)

    async def process_callback(self):
        """
        Trigger the update of the codebase index.
        """
        update_index_repository.si(self.project.path_with_namespace).apply_async()
