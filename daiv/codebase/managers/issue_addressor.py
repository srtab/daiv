import logging
from textwrap import dedent
from typing import TYPE_CHECKING, Literal, cast, override

from django.conf import settings as django_settings

from langchain_core.prompts import HumanMessagePromptTemplate
from langchain_core.prompts.string import jinja2_formatter
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from automation.agents.plan_and_execute import PlanAndExecuteAgent
from automation.agents.pr_describer import PullRequestDescriberAgent
from automation.agents.pr_describer.conf import settings as pr_describer_settings
from automation.utils import get_file_changes
from codebase.base import ClientType, FileChange, Issue
from core.constants import BOT_LABEL, BOT_NAME
from core.utils import generate_uuid

from .base import BaseManager
from .templates import (
    ISSUE_MERGE_REQUEST_TEMPLATE,
    ISSUE_NO_CHANGES_NEEDED_TEMPLATE,
    ISSUE_PLANNING_TEMPLATE,
    ISSUE_PROCESSED_TEMPLATE,
    ISSUE_QUESTIONS_TEMPLATE,
    ISSUE_REVIEW_PLAN_TEMPLATE,
    ISSUE_UNABLE_EXECUTE_PLAN_TEMPLATE,
    ISSUE_UNABLE_PROCESS_ISSUE_TEMPLATE,
)

if TYPE_CHECKING:
    from automation.agents.plan_and_execute.state import PlanAndExecuteState
    from codebase.context import RuntimeCtx

logger = logging.getLogger("daiv.managers")


ISSUE_ADDRESSING_TEMPLATE = HumanMessagePromptTemplate.from_template(
    """\
# TASK: {issue_title}

{issue_description}
"""
)  # noqa: E501


class IssueAddressorError(Exception):
    """
    Exception raised when the issue addressor encounters an error.
    """


class UnableToPlanIssueError(IssueAddressorError):
    """
    Exception raised when the agent is unable to plan the issue.

    """

    def __init__(self, *args, **kwargs):
        self.soft = kwargs.pop("soft", False)
        super().__init__(*args, **kwargs)


class UnableToExecutePlanError(IssueAddressorError):
    """
    Exception raised when the agent is unable to execute the plan.
    """


class IssueAddressorManager(BaseManager):
    """
    Manages the issue processing and addressing workflow.
    """

    def __init__(self, *, issue_iid: int, runtime_ctx: RuntimeCtx):
        super().__init__(runtime_ctx=runtime_ctx)
        self.issue: Issue = self.client.get_issue(self.ctx.repo_id, issue_iid)
        self.thread_id = generate_uuid(f"{self.ctx.repo_id}:{issue_iid}")

    @classmethod
    async def plan_issue(cls, *, issue_iid: int, runtime_ctx: RuntimeCtx, should_reset_plan: bool = False):
        """
        Plan the issue.

        Args:
            issue_iid (int): The issue ID.
            runtime_ctx (RuntimeCtx): The runtime context.
            should_reset_plan (bool): Whether to reset the plan.
        """
        manager = cls(issue_iid=issue_iid, runtime_ctx=runtime_ctx)

        try:
            await manager._plan_issue(should_reset_plan)
        except Exception as e:
            logger.exception("Error planning issue %d: %s", issue_iid, e)
            manager._add_unable_to_process_issue_note()

    @classmethod
    async def approve_plan(cls, *, issue_iid: int, runtime_ctx: RuntimeCtx):
        """
        Approve the plan for the given issue.

        Args:
            issue_iid: The issue ID.
            runtime_ctx: The runtime context.
        """
        manager = cls(issue_iid=issue_iid, runtime_ctx=runtime_ctx)

        try:
            await manager._approve_plan()
        except Exception:
            logger.exception("Error approving plan for issue %d", issue_iid)
            manager._add_unable_to_execute_plan_note()

    async def _plan_issue(self, should_reset_plan: bool):
        """
        Process the issue by addressing it with the appropriate actions.

        Args:
            should_reset_plan: Whether to reset the plan.
        """
        self._add_welcome_note()

        config = self._config

        async with AsyncPostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
            plan_and_execute = await PlanAndExecuteAgent.get_runnable(
                checkpointer=checkpointer, store=self._file_changes_store
            )
            current_state = None

            if should_reset_plan:
                await checkpointer.adelete_thread(self.thread_id)
            else:
                current_state = await plan_and_execute.aget_state(config)

            # If the plan needs to be reseted, or the agent has not been run yet, run it
            if should_reset_plan or (
                current_state is None or (not current_state.next and current_state.created_at is None)
            ):
                self._add_workflow_step_note("plan")

                human_message = await ISSUE_ADDRESSING_TEMPLATE.aformat(
                    issue_title=self.issue.title, issue_description=self.issue.description
                )
                result = await plan_and_execute.ainvoke({"messages": [human_message]}, config, context=self.ctx)

                self._handle_initial_result(result)

    def _handle_initial_result(self, state: PlanAndExecuteState):
        """
        Handle the initial result of the plan and execute agent.

        Args:
            state (PlanAndExecuteState): The state of the agent.
        """
        # We share the plan with the human to review and approve
        if plan_tasks := state.get("plan_tasks"):
            self._add_review_plan_note(plan_tasks)
        # We share the plan questions with the human to answer
        elif plan_questions := state.get("plan_questions"):
            self._add_plan_questions_note(plan_questions)
        # We share the no changes needed message with the human
        elif no_changes_needed := state.get("no_changes_needed"):
            self._add_no_changes_needed_note(no_changes_needed)
        # We share the final message from the agent with the human
        elif messages := state.get("messages"):
            self._create_or_update_comment(messages[-1].content)
        else:
            raise ValueError(f"Unexpected state returned: {state}")

    async def _approve_plan(self):
        """
        Approve the plan for the given issue.
        """
        async with AsyncPostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
            plan_and_execute = await PlanAndExecuteAgent.get_runnable(
                checkpointer=checkpointer, store=self._file_changes_store
            )

            current_state = await plan_and_execute.aget_state(self._config)

            if (
                "plan_approval" in current_state.next
                and current_state.interrupts
                or "execute_plan" in current_state.next
            ):
                self._add_workflow_step_note("execute_plan")

                await plan_and_execute.ainvoke(Command(resume="Plan approved"), self._config, context=self.ctx)
            else:
                self._add_no_plan_to_execute_note(bool(not current_state.next and current_state.created_at is not None))

            if file_changes := await get_file_changes(self._file_changes_store):
                if merge_request_id := await self._commit_changes(file_changes=file_changes, thread_id=self.thread_id):
                    self._add_issue_processed_note(merge_request_id)
            else:
                after_run_state = await plan_and_execute.aget_state(self._config)

                if no_changes_needed := after_run_state.values.get("no_changes_needed"):
                    self._add_no_changes_needed_note(no_changes_needed)

    @property
    def _config(self):
        """
        Get the config for the agent.
        """
        return RunnableConfig(
            tags=[str(self.client.client_slug)],
            metadata={"author": self.issue.author.username, "issue_id": self.issue.iid},
            configurable={"thread_id": self.thread_id, "bot_username": self.ctx.bot_username},
        )

    @override
    async def _commit_changes(
        self, *, file_changes: list[FileChange], thread_id: str | None = None, skip_ci: bool = False
    ) -> int | str | None:
        """
        Process file changes and create or update merge request.

        Args:
            file_changes: The file changes to commit.
            thread_id: The thread ID.
            skip_ci: Whether to skip the CI.
        """
        pr_describer = await PullRequestDescriberAgent.get_runnable()
        changes_description = await pr_describer.ainvoke(
            {
                "changes": file_changes,
                "extra_context": dedent(
                    """\
                    This changes were made to address the following issue:

                    Issue title: {title}
                    Issue description: {description}
                    """
                ).format(title=self.issue.title, description=self.issue.description),
                "branch_name_convention": self.ctx.config.pull_request.branch_name_convention,
            },
            config=RunnableConfig(
                tags=[pr_describer_settings.NAME, str(self.client.client_slug)], configurable={"thread_id": thread_id}
            ),
        )
        merge_requests = self.client.get_issue_related_merge_requests(
            self.ctx.repo_id, cast("int", self.issue.iid), label=BOT_LABEL
        )

        if merge_requests:
            changes_description.branch = merge_requests[0].source_branch
        else:
            changes_description.branch = self._get_unique_branch_name(changes_description.branch)

        commit_message = changes_description.commit_message
        if skip_ci:
            commit_message = f"[skip ci] {commit_message}"

        self.client.commit_changes(
            self.ctx.repo_id,
            changes_description.branch,
            commit_message,
            file_changes,
            start_branch=self.ctx.ref,
            override_commits=True,
        )

        if self.issue.assignee:
            assignee_id = (
                self.issue.assignee.id if self.client.client_slug == ClientType.GITLAB else self.issue.assignee.username
            )
        else:
            assignee_id = None

        return self.client.update_or_create_merge_request(
            repo_id=self.ctx.repo_id,
            source_branch=changes_description.branch,
            target_branch=self.ctx.ref,
            labels=[BOT_LABEL],
            title=changes_description.title,
            assignee_id=assignee_id,
            description=jinja2_formatter(
                ISSUE_MERGE_REQUEST_TEMPLATE,
                description=changes_description.description,
                summary=changes_description.summary,
                source_repo_id=self.ctx.repo_id,
                issue_id=self.issue.iid,
                bot_name=BOT_NAME,
                bot_username=self.ctx.bot_username,
                is_gitlab=self.client.client_slug == ClientType.GITLAB,
            ),
        )

    def _add_welcome_note(self):
        """
        Leave a welcome note if the issue has no bot comment.
        """
        if not any(note.author.id == self.client.current_user.id for note in self.issue.notes):
            self.client.create_issue_comment(
                self.ctx.repo_id,
                cast("int", self.issue.iid),
                jinja2_formatter(
                    ISSUE_PLANNING_TEMPLATE,
                    assignee=self.issue.assignee.username if self.issue.assignee else None,
                    bot_name=BOT_NAME,
                ),
            )

    def _add_review_plan_note(self, plan_tasks: list[dict]):
        """
        Add a note to the issue to inform the user that the plan has been reviewed and is ready for approval.

        Args:
            plan_tasks: The plan tasks.
        """
        from quick_actions.actions.plan import ApprovePlanQuickAction

        self._create_or_update_comment(
            jinja2_formatter(
                ISSUE_REVIEW_PLAN_TEMPLATE,
                plan_tasks=plan_tasks,
                approve_plan_command=ApprovePlanQuickAction().command_to_activate,
            )
        )

    def _add_unable_to_process_issue_note(self):
        """
        Add a note to the issue to inform the user that the issue could not be processed.
        """
        from quick_actions.actions.plan import RevisePlanQuickAction

        self._create_or_update_comment(
            jinja2_formatter(
                ISSUE_UNABLE_PROCESS_ISSUE_TEMPLATE,
                bot_name=BOT_NAME,
                revise_plan_command=RevisePlanQuickAction().command_to_activate,
            )
        )

    def _add_unable_to_execute_plan_note(self):
        """
        Add a note to the issue to inform the user that the plan could not be executed.
        """
        from quick_actions.actions.plan import ApprovePlanQuickAction

        self._create_or_update_comment(
            jinja2_formatter(
                ISSUE_UNABLE_EXECUTE_PLAN_TEMPLATE,
                bot_name=BOT_NAME,
                approve_plan_command=ApprovePlanQuickAction().command_to_activate,
            )
        )

    def _add_issue_processed_note(self, merge_request_id: int):
        """
        Add a note to the issue to inform the user that the issue has been processed.
        """
        self._create_or_update_comment(
            jinja2_formatter(
                ISSUE_PROCESSED_TEMPLATE,
                source_repo_id=self.ctx.repo_id,
                merge_request_id=merge_request_id,
                # GitHub already shows the merge request link right after the comment.
                show_merge_request_link=self.client.client_slug == ClientType.GITLAB,
            )
        )

    def _add_plan_questions_note(self, plan_questions: list[str]):
        """
        Add a note to the issue to inform the user that the plan has questions.
        """
        self._create_or_update_comment(jinja2_formatter(ISSUE_QUESTIONS_TEMPLATE, questions=plan_questions))

    def _add_no_changes_needed_note(self, no_changes_needed: str):
        """
        Add a note to the issue to inform the user that the plan has no changes needed.
        """
        self._create_or_update_comment(
            jinja2_formatter(ISSUE_NO_CHANGES_NEEDED_TEMPLATE, no_changes_needed=no_changes_needed)
        )

    def _add_no_plan_to_execute_note(self, already_executed: bool = False):
        """
        Add a note to the issue to inform the user that the plan could not be executed.
        """
        self._create_or_update_comment(
            "ℹ️ The plan has already been executed." if already_executed else "ℹ️ No pending plan to be executed."
        )

    def _add_workflow_step_note(self, step_name: Literal["plan", "execute_plan"]):
        """
        Add a note to the discussion that the workflow step is in progress.

        Args:
            step_name: The name of the step
        """
        if step_name == "plan":
            note_message = "🛠️ Analyzing the issue and drafting a detailed plan to address it — *in progress* ..."
        elif step_name == "execute_plan":
            note_message = "🚀 Executing the plan to address the issue — *in progress* ..."

        self._create_or_update_comment(note_message)

    def _create_or_update_comment(self, note_message: str):
        """
        Create or update a comment on the issue.
        """
        if self._comment_id is not None:
            self.client.update_issue_comment(self.ctx.repo_id, self.issue.iid, self._comment_id, note_message)
        else:
            self._comment_id = self.client.create_issue_comment(self.ctx.repo_id, self.issue.iid, note_message)
