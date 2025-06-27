import logging
from textwrap import dedent
from typing import cast, override

from django.conf import settings as django_settings

from langchain_core.prompts.string import jinja2_formatter
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import merge_configs
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.types import INTERRUPT
from langgraph.types import Command

from automation.agents.issue_addressor import IssueAddressorAgent
from automation.agents.issue_addressor.conf import settings as issue_addressor_settings
from automation.agents.issue_addressor.templates import (
    ISSUE_MERGE_REQUEST_TEMPLATE,
    ISSUE_PLANNING_TEMPLATE,
    ISSUE_PROCESSED_TEMPLATE,
    ISSUE_QUESTIONS_TEMPLATE,
    ISSUE_REVIEW_PLAN_TEMPLATE,
    ISSUE_REVISE_TEMPLATE,
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


class NoPlanToExecuteError(IssueAddressorError):
    """
    Exception raised when the agent is unable to execute the plan.
    """


class IssueAddressorManager(BaseManager):
    """
    Manages the issue processing and addressing workflow.
    """

    def __init__(self, repo_id: str, issue_iid: int, ref: str | None = None):
        super().__init__(RepoClient.create_instance(), repo_id, ref)
        self.repository = self.client.get_repository(repo_id)
        self.thread_id = generate_uuid(f"{repo_id}{issue_iid}")
        self.issue: Issue = self.client.get_issue(repo_id, issue_iid)

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
        manager = cls(repo_id, issue_iid, ref)

        try:
            await manager._plan_issue(should_reset_plan, discussion_id)
        except UnableToPlanIssueError as e:
            if e.soft:
                logger.warning("Soft error planning issue %d: %s", issue_iid, e)
            else:
                logger.exception("Error planning issue %d: %s", issue_iid, e)
            note_message = jinja2_formatter(ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE, discussion_id=discussion_id)
            if discussion_id:
                manager.client.create_issue_discussion_note(repo_id, issue_iid, note_message, discussion_id)
            else:
                manager.client.comment_issue(repo_id, issue_iid, note_message)
        except Exception as e:
            logger.exception("Error processing issue %d: %s", issue_iid, e)
            manager._add_unable_to_process_issue_note(discussion_id)

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
        manager = cls(repo_id, issue_iid, ref)

        try:
            await manager._approve_plan(discussion_id)
        except Exception:
            logger.exception("Error approving plan for issue %d", issue_iid)
            manager._add_unable_to_process_issue_note(discussion_id)

    async def _plan_issue(self, should_reset_plan: bool, discussion_id: str | None = None):
        """
        Process the issue by addressing it with the appropriate actions.

        Args:
            should_reset_plan: Whether to reset the plan.
            discussion_id: The discussion ID of the note that triggered the action.
        """
        self._add_welcome_note()

        config = self._config

        async with AsyncPostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
            issue_addressor = IssueAddressorAgent(checkpointer=checkpointer, store=self._file_changes_store)
            agent = await issue_addressor.agent

            if should_reset_plan and (
                history_states := [state async for state in agent.aget_state_history(config, filter={"step": -1})]
            ):
                # Rollback to the first state to reset the state of the agent
                config = merge_configs(config, history_states[-1].config)
                self._add_plan_revised_note(discussion_id)

            current_state = await agent.aget_state(config, subgraphs=True)

            # If the plan needs to be reseted, or the agent has not been run yet, run it
            if should_reset_plan or (not current_state.next and current_state.created_at is None):
                try:
                    result = await agent.ainvoke(
                        {"issue_title": self.issue.title, "issue_description": self.issue.description}, config
                    )
                except Exception as e:
                    raise UnableToPlanIssueError("Error planning issue") from e
                else:
                    # The first task is the plan_and_execute node
                    self._handle_initial_result(INTERRUPT in result and result[INTERRUPT][0].value or result)

    def _handle_initial_result(self, state: dict, discussion_id: str | None = None):
        """
        Handle the initial state of issue processing.

        Args:
            state: The state of the agent.
            discussion_id: The discussion ID of the note that triggered the action.
        """
        if state.get("request_for_changes") is False:
            raise UnableToPlanIssueError("No plan was generated.", soft=True)
        # We share the plan with the human to review and approve
        elif plan_tasks := state.get("plan_tasks"):
            self._add_review_plan_note(plan_tasks)
        # We share the questions with the human to answer
        elif plan_questions := state.get("plan_questions"):
            self._add_plan_questions_note(plan_questions, discussion_id)
        else:
            raise UnableToPlanIssueError("Unexpected state from plan and execute node")

    async def _approve_plan(self, discussion_id: str | None = None):
        """
        Approve the plan for the given issue.

        Args:
            discussion_id: The discussion ID of the note that triggered the action.
        """
        async with AsyncPostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
            issue_addressor = IssueAddressorAgent(checkpointer=checkpointer, store=self._file_changes_store)
            agent = await issue_addressor.agent

            current_state = await agent.aget_state(self._config, subgraphs=True)

            # If the agent is waiting for the human to approve the plan on the sub-graph of the plan_and_execute node,
            # We resume the execution of the plan_and_execute node.
            if "plan_and_execute" in current_state.next and "plan_approval" in current_state.tasks[0].state.next:
                try:
                    if discussion_id:
                        note_message = "I'll apply the plan straight away.\n\nI'll let you know when it's done."
                        self.client.create_issue_discussion_note(
                            self.repo_id, self.issue.iid, note_message, discussion_id
                        )

                    await agent.ainvoke(Command(resume="Plan approved"), self._config)
                except Exception:
                    logger.exception("Error executing plan for issue %d", self.issue.iid)
                    self._add_unable_to_execute_plan_note(discussion_id)
            else:
                if not current_state.next:
                    note_message = "The plan has already been executed."
                else:
                    note_message = "There's no plan to be executed."

                if discussion_id:
                    self.client.create_issue_discussion_note(self.repo_id, self.issue.iid, note_message, discussion_id)
                else:
                    self.client.comment_issue(self.repo_id, cast("int", self.issue.iid), note_message)

            if (file_changes := await self._get_file_changes()) and (
                merge_request_id := await self._commit_changes(file_changes=file_changes, thread_id=self.thread_id)
            ):
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
        Add a note to the issue to inform the user that the plan has been reviewed.
        """
        self.client.comment_issue(
            self.repo_id,
            cast("int", self.issue.iid),
            jinja2_formatter(
                ISSUE_REVIEW_PLAN_TEMPLATE, plan_tasks=plan_tasks, bot_username=self.client.current_user.username
            ),
        )

    def _add_plan_revised_note(self, discussion_id: str | None = None):
        """
        Add a note to the issue to inform the user that the plan has been revised.
        """
        note_message = jinja2_formatter(ISSUE_REVISE_TEMPLATE, discussion_id=discussion_id)
        if discussion_id:
            self.client.create_issue_discussion_note(
                self.repo_id, cast("int", self.issue.iid), note_message, discussion_id
            )
        else:
            self.client.comment_issue(self.repo_id, cast("int", self.issue.iid), note_message)

    def _add_unable_to_process_issue_note(self, discussion_id: str | None = None):
        """
        Add a note to the issue to inform the user that the issue could not be processed.
        """
        note_message = jinja2_formatter(ISSUE_UNABLE_PROCESS_ISSUE_TEMPLATE, discussion_id=discussion_id)
        if discussion_id:
            self.client.create_issue_discussion_note(self.repo_id, self.issue.iid, note_message, discussion_id)
        else:
            self.client.comment_issue(self.repo_id, self.issue.iid, note_message)

    def _add_unable_to_execute_plan_note(self, discussion_id: str | None = None):
        """
        Add a note to the issue to inform the user that the plan could not be executed.
        """
        note_message = jinja2_formatter(ISSUE_UNABLE_EXECUTE_PLAN_TEMPLATE, discussion_id=discussion_id)
        if discussion_id:
            self.client.create_issue_discussion_note(self.repo_id, self.issue.iid, note_message, discussion_id)
        else:
            self.client.comment_issue(self.repo_id, cast("int", self.issue.iid), note_message)

    def _add_issue_processed_note(self, merge_request_id: int):
        """
        Add a note to the issue to inform the user that the issue has been processed.
        """
        note_message = jinja2_formatter(
            ISSUE_PROCESSED_TEMPLATE, source_repo_id=self.repo_id, merge_request_id=merge_request_id
        )
        self.client.comment_issue(self.repo_id, self.issue.iid, note_message)

    def _add_plan_questions_note(self, plan_questions: list[str], discussion_id: str | None = None):
        """
        Add a note to the issue to inform the user that the plan has questions.
        """
        note_message = jinja2_formatter(ISSUE_QUESTIONS_TEMPLATE, questions=plan_questions, discussion_id=discussion_id)
        if discussion_id:
            self.client.create_issue_discussion_note(
                self.repo_id, cast("int", self.issue.iid), note_message, discussion_id
            )
        else:
            self.client.comment_issue(self.repo_id, cast("int", self.issue.iid), note_message)
