import logging
from textwrap import dedent
from typing import Literal, cast, override

from django.conf import settings as django_settings

from langchain_core.prompts.string import jinja2_formatter
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from automation.agents.issue_addressor import IssueAddressorAgent
from automation.agents.issue_addressor.conf import settings as issue_addressor_settings
from automation.agents.issue_addressor.templates import (
    ISSUE_MERGE_REQUEST_TEMPLATE,
    ISSUE_PLANNING_TEMPLATE,
    ISSUE_PROCESSED_TEMPLATE,
    ISSUE_QUESTIONS_TEMPLATE,
    ISSUE_REVIEW_PLAN_TEMPLATE,
    ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE,
    ISSUE_UNABLE_EXECUTE_PLAN_TEMPLATE,
    ISSUE_UNABLE_PROCESS_ISSUE_TEMPLATE,
)
from automation.agents.pr_describer import PullRequestDescriberAgent
from automation.agents.pr_describer.conf import settings as pr_describer_settings
from codebase.base import FileChange, Issue
from codebase.clients import RepoClient
from core.constants import BOT_LABEL, BOT_NAME
from core.utils import generate_uuid

from .base import BaseManager

logger = logging.getLogger("daiv.managers")


EXECUTE_PLAN_COMMAND = "@{bot_username} plan execute"
REVISE_PLAN_COMMAND = "@{bot_username} plan revise"


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
        super().__init__(RepoClient.create_instance(), repo_id, ref)
        self.repository = self.client.get_repository(repo_id)
        self.issue: Issue = self.client.get_issue(repo_id, issue_iid)
        self.discussion_id = discussion_id
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
            discussion_id: The discussion ID of the note that triggered the action.
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
            issue_addressor = await IssueAddressorAgent(checkpointer=checkpointer, store=self._file_changes_store).agent
            current_state = None

            if should_reset_plan:
                await checkpointer.adelete_thread(self.thread_id)
            else:
                current_state = await issue_addressor.aget_state(config, subgraphs=True)

            # If the plan needs to be reseted, or the agent has not been run yet, run it
            if should_reset_plan or (
                current_state is None or (not current_state.next and current_state.created_at is None)
            ):
                async for event in issue_addressor.astream_events(
                    {"issue_title": self.issue.title, "issue_description": self.issue.description},
                    config,
                    include_names=["plan_and_execute"],
                    include_types=["on_chain_start"],
                ):
                    if event["event"] == "on_chain_start":
                        self._add_workflow_step_note(event["name"], planning=True)

                after_run_state = await issue_addressor.aget_state(config, subgraphs=True)

                if "plan_and_execute" in after_run_state.next and after_run_state.interrupts:
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
        else:
            raise ValueError(f"Unexpected state returned: {state}")

    async def _approve_plan(self):
        """
        Approve the plan for the given issue.
        """
        async with AsyncPostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
            issue_addressor = IssueAddressorAgent(checkpointer=checkpointer, store=self._file_changes_store)

            current_state = await issue_addressor.agent.aget_state(self._config, subgraphs=True)

            # If the agent is waiting for the human to approve the plan on the sub-graph of the plan_and_execute node,
            # We resume the execution of the plan_and_execute node.
            if "plan_and_execute" in current_state.next and current_state.interrupts:
                async for event in issue_addressor.agent.astream_events(
                    Command(resume="Plan approved"),
                    self._config,
                    include_names=["plan_and_execute", "apply_format_code"],
                    include_types=["on_chain_start"],
                ):
                    if event["event"] == "on_chain_start":
                        self._add_workflow_step_note(event["name"])
            else:
                self._add_no_plan_to_execute_note(bool(not current_state.next and current_state.created_at is not None))

            if file_changes := await self._get_file_changes():
                self._add_workflow_step_note("commit_changes")
                if merge_request_id := await self._commit_changes(file_changes=file_changes, thread_id=self.thread_id):
                    self._add_issue_processed_note(merge_request_id)

    @property
    def _config(self):
        """
        Get the config for the agent.
        """
        return RunnableConfig(
            tags=[issue_addressor_settings.NAME, str(self.client.client_slug)],
            metadata={"author": self.issue.author.username},
            configurable={
                "thread_id": self.thread_id,
                "project_id": self.repository.pk,
                "source_repo_id": self.repo_id,
                "source_ref": self.ref,
                "issue_id": self.issue.iid,
                "repo_client": self.client.client_slug,
            },
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
        pr_describer = await PullRequestDescriberAgent().agent
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
                "branch_name_convention": self.repo_config.branch_name_convention,
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
            self.client.comment_issue(
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
        note_message = "✅ **Plan Ready for Review** - Please check the implementation plan below."
        if self.discussion_id:
            self.client.create_issue_discussion_note(self.repo_id, self.issue.iid, note_message, self.discussion_id)
        else:
            self.client.comment_issue(self.repo_id, self.issue.iid, note_message)

        self.client.comment_issue(
            self.repo_id,
            cast("int", self.issue.iid),
            jinja2_formatter(
                ISSUE_REVIEW_PLAN_TEMPLATE,
                plan_tasks=plan_tasks,
                approve_plan_command=EXECUTE_PLAN_COMMAND.format(bot_username=self.client.current_user.username),
            ),
        )

    def _add_unable_to_define_plan_note(self):
        """
        Add a note to the issue to inform the user that the plan could not be defined.
        """
        note_message = ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE
        if self.discussion_id:
            self.client.create_issue_discussion_note(self.repo_id, self.issue.iid, note_message, self.discussion_id)
        else:
            self.client.comment_issue(self.repo_id, self.issue.iid, note_message)

    def _add_unable_to_process_issue_note(self):
        """
        Add a note to the issue to inform the user that the issue could not be processed.
        """
        note_message = jinja2_formatter(
            ISSUE_UNABLE_PROCESS_ISSUE_TEMPLATE,
            bot_name=BOT_NAME,
            revise_plan_command=REVISE_PLAN_COMMAND.format(bot_username=self.client.current_user.username),
        )
        if self.discussion_id:
            self.client.create_issue_discussion_note(self.repo_id, self.issue.iid, note_message, self.discussion_id)
        else:
            self.client.comment_issue(self.repo_id, self.issue.iid, note_message)

    def _add_unable_to_execute_plan_note(self):
        """
        Add a note to the issue to inform the user that the plan could not be executed.
        """
        note_message = jinja2_formatter(
            ISSUE_UNABLE_EXECUTE_PLAN_TEMPLATE,
            bot_name=BOT_NAME,
            execute_plan_command=EXECUTE_PLAN_COMMAND.format(bot_username=self.client.current_user.username),
        )
        if self.discussion_id:
            self.client.create_issue_discussion_note(self.repo_id, self.issue.iid, note_message, self.discussion_id)
        else:
            self.client.comment_issue(self.repo_id, cast("int", self.issue.iid), note_message)

    def _add_issue_processed_note(self, merge_request_id: int):
        """
        Add a note to the issue to inform the user that the issue has been processed.
        """
        note_message = jinja2_formatter(
            ISSUE_PROCESSED_TEMPLATE, source_repo_id=self.repo_id, merge_request_id=merge_request_id
        )
        if self.discussion_id:
            self.client.create_issue_discussion_note(self.repo_id, self.issue.iid, note_message, self.discussion_id)
        else:
            self.client.comment_issue(self.repo_id, self.issue.iid, note_message)

    def _add_plan_questions_note(self, plan_questions: list[str]):
        """
        Add a note to the issue to inform the user that the plan has questions.
        """
        note_message = jinja2_formatter(ISSUE_QUESTIONS_TEMPLATE, questions=plan_questions)
        if self.discussion_id:
            self.client.create_issue_discussion_note(
                self.repo_id, cast("int", self.issue.iid), note_message, self.discussion_id
            )
        else:
            self.client.comment_issue(self.repo_id, cast("int", self.issue.iid), note_message)

    def _add_no_plan_to_execute_note(self, already_executed: bool = False):
        """
        Add a note to the issue to inform the user that the plan could not be executed.
        """
        note_message = (
            "ℹ️ The plan has already been executed." if already_executed else "ℹ️ No pending plan to be executed."
        )
        if self.discussion_id:
            self.client.create_issue_discussion_note(self.repo_id, self.issue.iid, note_message, self.discussion_id)
        else:
            self.client.comment_issue(self.repo_id, cast("int", self.issue.iid), note_message)

    def _add_workflow_step_note(
        self, step_name: Literal["plan_and_execute", "apply_format_code", "commit_changes"], planning: bool = False
    ):
        """
        Add a note to the discussion that the workflow step is in progress.

        Args:
            step_name: The name of the step
            planning: Whether the step is planning or executing
        """
        if step_name == "plan_and_execute":
            if planning:
                note_message = "🛠️ Drafting a detailed plan to address the issue — *in progress* ..."
            else:
                note_message = "🚀 Executing the plan to address the issue — *in progress* ..."
        elif step_name == "apply_format_code":
            note_message = "🎨 Formatting code — *in progress* ..."
        elif step_name == "commit_changes":
            note_message = "💾 Committing code changes — *in progress* ..."

        if self.discussion_id:
            self.client.create_issue_discussion_note(self.repo_id, self.issue.iid, note_message, self.discussion_id)
        else:
            self.client.comment_issue(self.repo_id, cast("int", self.issue.iid), note_message)
