import logging
from textwrap import dedent
from typing import Literal, cast, override

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
from codebase.base import FileChange, Issue
from codebase.clients import RepoClient
from codebase.repo_config import RepositoryConfig
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
    ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE,
    ISSUE_UNABLE_EXECUTE_PLAN_TEMPLATE,
    ISSUE_UNABLE_PROCESS_ISSUE_TEMPLATE,
)

logger = logging.getLogger("daiv.managers")


EXECUTE_PLAN_COMMAND = "@{bot_username} plan execute"
REVISE_PLAN_COMMAND = "@{bot_username} plan revise"

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

    def __init__(self, repo_id: str, issue_iid: int, ref: str | None = None, discussion_id: str | None = None):
        super().__init__(RepoClient.create_instance(), repo_id, ref, discussion_id)
        self.repository = self.client.get_repository(repo_id)
        self.repo_config = RepositoryConfig.get_config(repo_id)
        self.issue: Issue = self.client.get_issue(repo_id, issue_iid)
        self.thread_id = generate_uuid(f"{repo_id}{issue_iid}")

    @classmethod
    async def plan_issue(
        cls,
        repo_id: str,
        issue_iid: int,
        ref: str | None = None,
        should_reset_plan: bool = False,
        discussion_id: str | None = None,
    ):
        """
        Plan the issue.

        Args:
            repo_id: The repository ID.
            issue_iid: The issue ID.
            ref: The reference branch.
            should_reset_plan: Whether to reset the plan.
            discussion_id: The discussion ID.
        """
        manager = cls(repo_id, issue_iid, ref, discussion_id)

        try:
            await manager._plan_issue(should_reset_plan)
        except UnableToPlanIssueError as e:
            if e.soft:
                logger.warning("Soft error planning issue %d: %s", issue_iid, e)
            else:
                logger.exception("Error planning issue %d: %s", issue_iid, e)
            manager._add_unable_to_define_plan_note()
        except Exception as e:
            logger.exception("Error processing issue %d: %s", issue_iid, e)
            manager._add_unable_to_process_issue_note()

    @classmethod
    async def approve_plan(cls, repo_id: str, issue_iid: int, ref: str | None = None, discussion_id: str | None = None):
        """
        Approve the plan for the given issue.

        Args:
            repo_id: The repository ID.
            issue_iid: The issue ID.
            ref: The reference branch.
            discussion_id: The discussion ID.
        """
        manager = cls(repo_id, issue_iid, ref, discussion_id)

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
                human_message = await ISSUE_ADDRESSING_TEMPLATE.aformat(
                    issue_title=self.issue.title, issue_description=self.issue.description
                )
                async for event in plan_and_execute.astream_events(
                    {"messages": [human_message]},
                    config,
                    include_names=["pre_plan", "plan"],
                    include_types=["on_chain_start"],
                ):
                    if event["event"] == "on_chain_start":
                        self._add_workflow_step_note(event["name"])

                after_run_state = await plan_and_execute.aget_state(config)

                if "plan_approval" in after_run_state.next and after_run_state.interrupts:
                    values = after_run_state.interrupts[0].value
                else:
                    values = after_run_state.values

                self._handle_initial_result(values)

    def _handle_initial_result(self, state: dict):
        """
        Handle the initial state of issue processing.

        Args:
            state: The state of the agent.
        """
        if state.get("request_for_changes") is False:
            raise UnableToPlanIssueError("The issue is not a request for changes.", soft=True)
        # We share the plan with the human to review and approve
        elif plan_tasks := state.get("plan_tasks"):
            self._add_review_plan_note(plan_tasks)
        # We share the questions with the human to answer
        elif plan_questions := state.get("plan_questions"):
            self._add_plan_questions_note(plan_questions)
        elif no_changes_needed := state.get("no_changes_needed"):
            self._add_no_changes_needed_note(no_changes_needed)
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
                async for event in plan_and_execute.astream_events(
                    Command(resume="Plan approved"),
                    self._config,
                    include_names=["execute_plan", "apply_format_code"],
                    include_types=["on_chain_start"],
                ):
                    if event["event"] == "on_chain_start":
                        self._add_workflow_step_note(event["name"])
            else:
                self._add_no_plan_to_execute_note(bool(not current_state.next and current_state.created_at is not None))

            if file_changes := await get_file_changes(self._file_changes_store):
                self._add_workflow_step_note("commit_changes")

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
            configurable={"thread_id": self.thread_id, "bot_username": self.client.current_user.username},
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
                "branch_name_convention": self.repo_config.pull_request.branch_name_convention,
            },
            config=RunnableConfig(
                tags=[pr_describer_settings.NAME, str(self.client.client_slug)], configurable={"thread_id": thread_id}
            ),
        )
        merge_requests = self.client.get_issue_related_merge_requests(
            self.repo_id, cast("int", self.issue.iid), label=BOT_LABEL
        )

        if merge_requests:
            changes_description.branch = merge_requests[0].source_branch
        else:
            changes_description.branch = self._get_unique_branch_name(changes_description.branch)

        commit_message = changes_description.commit_message
        if skip_ci:
            commit_message = f"[skip ci] {commit_message}"

        self.client.commit_changes(
            self.repo_id,
            changes_description.branch,
            commit_message,
            file_changes,
            start_branch=self.ref,
            override_commits=True,
        )

        return self.client.update_or_create_merge_request(
            repo_id=self.repo_id,
            source_branch=changes_description.branch,
            target_branch=self.ref,
            labels=[BOT_LABEL],
            title=changes_description.title,
            assignee_id=self.issue.assignee.id if self.issue.assignee else None,
            description=jinja2_formatter(
                ISSUE_MERGE_REQUEST_TEMPLATE,
                description=changes_description.description,
                summary=changes_description.summary,
                source_repo_id=self.repo_id,
                issue_id=self.issue.iid,
                bot_name=BOT_NAME,
                bot_username=self.client.current_user.username,
            ),
        )

    def _add_welcome_note(self):
        """
        Leave a welcome note if the issue has no bot comment.
        """
        if not any(note.author.id == self.client.current_user.id for note in self.issue.notes):
            self.client.create_issue_comment(
                self.repo_id,
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
        self._create_or_update_comment(
            jinja2_formatter(
                ISSUE_REVIEW_PLAN_TEMPLATE,
                plan_tasks=plan_tasks,
                approve_plan_command=EXECUTE_PLAN_COMMAND.format(bot_username=self.client.current_user.username),
            )
        )

    def _add_unable_to_define_plan_note(self):
        """
        Add a note to the issue to inform the user that the plan could not be defined.
        """
        self._create_or_update_comment(ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE)

    def _add_unable_to_process_issue_note(self):
        """
        Add a note to the issue to inform the user that the issue could not be processed.
        """
        self._create_or_update_comment(
            jinja2_formatter(
                ISSUE_UNABLE_PROCESS_ISSUE_TEMPLATE,
                bot_name=BOT_NAME,
                revise_plan_command=REVISE_PLAN_COMMAND.format(bot_username=self.client.current_user.username),
            )
        )

    def _add_unable_to_execute_plan_note(self):
        """
        Add a note to the issue to inform the user that the plan could not be executed.
        """
        self._create_or_update_comment(
            jinja2_formatter(
                ISSUE_UNABLE_EXECUTE_PLAN_TEMPLATE,
                bot_name=BOT_NAME,
                execute_plan_command=EXECUTE_PLAN_COMMAND.format(bot_username=self.client.current_user.username),
            )
        )

    def _add_issue_processed_note(self, merge_request_id: int):
        """
        Add a note to the issue to inform the user that the issue has been processed.
        """
        self._create_or_update_comment(
            jinja2_formatter(ISSUE_PROCESSED_TEMPLATE, source_repo_id=self.repo_id, merge_request_id=merge_request_id)
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

    def _add_workflow_step_note(
        self, step_name: Literal["pre_plan", "plan", "execute_plan", "apply_format_code", "commit_changes"]
    ):
        """
        Add a note to the discussion that the workflow step is in progress.

        Args:
            step_name: The name of the step
        """
        if step_name == "pre_plan":
            note_message = "🔍 Analyzing the issue and preparing the necessary data — *in progress* ..."
        elif step_name == "plan":
            note_message = "🛠️ Drafting a detailed plan to address the issue — *in progress* ..."
        elif step_name == "execute_plan":
            note_message = "🚀 Executing the plan to address the issue — *in progress* ..."
        elif step_name == "apply_format_code":
            note_message = "🎨 Formatting code — *in progress* ..."
        elif step_name == "commit_changes":
            note_message = "💾 Committing code changes — *in progress* ..."

        self._create_or_update_comment(note_message)
