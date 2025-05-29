import logging
from textwrap import dedent
from typing import cast, override

from django.conf import settings as django_settings

from langchain_core.prompts.string import jinja2_formatter
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import merge_configs
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.types import INTERRUPT
from langgraph.types import Command

from automation.agents.issue_addressor import IssueAddressorAgent
from automation.agents.issue_addressor.conf import settings as issue_addressor_settings
from automation.agents.issue_addressor.templates import (
    ISSUE_MERGE_REQUEST_TEMPLATE,
    ISSUE_PLANNING_TEMPLATE,
    ISSUE_PROCESSED_TEMPLATE,
    ISSUE_QUESTIONS_TEMPLATE,
    ISSUE_REPLAN_TEMPLATE,
    ISSUE_REVIEW_PLAN_TEMPLATE,
    ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE,
    ISSUE_UNABLE_EXECUTE_PLAN_TEMPLATE,
    ISSUE_UNABLE_PROCESS_ISSUE_TEMPLATE,
)
from automation.agents.pr_describer import PullRequestDescriberAgent
from automation.agents.pr_describer.conf import settings as pr_describer_settings
from codebase.base import FileChange, Issue
from codebase.clients import AllRepoClient, RepoClient
from codebase.utils import notes_to_messages
from core.constants import BOT_LABEL, BOT_NAME
from core.utils import generate_uuid

from .base import BaseManager

logger = logging.getLogger("daiv.managers")


class IssueAddressorError(Exception):
    """
    Exception raised when the issue addressor encounters an error.
    """

    pass


class UnableToPlanIssueError(IssueAddressorError):
    """
    Exception raised when the agent is unable to plan the issue.
    """

    pass


class UnableToExecutePlanError(IssueAddressorError):
    """
    Exception raised when the agent is unable to execute the plan.
    """

    pass


class IssueAddressorManager(BaseManager):
    """
    Manages the issue processing and addressing workflow.
    """

    def __init__(self, client: AllRepoClient, repo_id: str, ref: str | None = None, **kwargs):
        super().__init__(client, repo_id, ref)
        self.repository = self.client.get_repository(repo_id)
        self.thread_id = kwargs["thread_id"]
        self.issue: Issue = kwargs["issue"]

    @classmethod
    def process_issue(cls, repo_id: str, issue_iid: int, ref: str | None = None, should_reset_plan: bool = False):
        """
        Process an issue by creating a merge request with the changes described in the issue description.

        Args:
            repo_id: The repository ID.
            issue_iid: The issue ID.
            ref: The reference branch.
            should_reset_plan: Whether to reset the plan.
        """
        client = RepoClient.create_instance()

        thread_id = generate_uuid(f"{repo_id}{issue_iid}")
        issue = client.get_issue(repo_id, issue_iid)

        manager = cls(client, repo_id, ref, issue=issue, thread_id=thread_id)

        try:
            manager._process_issue(should_reset_plan)
        except UnableToPlanIssueError as e:
            logger.exception("Error planning issue %d: %s", issue_iid, e)
            client.comment_issue(repo_id, issue_iid, ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE)
        except UnableToExecutePlanError as e:
            logger.exception("Error executing plan %d: %s", issue_iid, e)
            client.comment_issue(repo_id, issue_iid, ISSUE_UNABLE_EXECUTE_PLAN_TEMPLATE)
        except Exception as e:
            logger.exception("Error processing issue %d: %s", issue_iid, e)
            client.comment_issue(repo_id, issue_iid, ISSUE_UNABLE_PROCESS_ISSUE_TEMPLATE)

    def _process_issue(self, should_reset_plan: bool):
        """
        Process the issue by addressing it with the appropriate actions.

        Args:
            should_reset_plan: Whether to reset the plan.
        """
        config = RunnableConfig(
            recursion_limit=issue_addressor_settings.RECURSION_LIMIT,
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

        self._add_welcome_note()

        with PostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
            issue_addressor = IssueAddressorAgent(checkpointer=checkpointer, store=self._file_changes_store)

            if should_reset_plan and (
                history_states := list(issue_addressor.agent.get_state_history(config, filter={"step": -1}))
            ):
                # Rollback to the first state to reset the state of the agent
                config = merge_configs(config, history_states[-1].config)
                self.client.comment_issue(self.repo_id, cast("int", self.issue.iid), ISSUE_REPLAN_TEMPLATE)

            current_state = issue_addressor.agent.get_state(config, subgraphs=True)

            # If the plan needs to be reseted, or the agent has not been run yet, run it
            if should_reset_plan or (not current_state.next and current_state.created_at is None):
                try:
                    result = issue_addressor.agent.invoke(
                        {"issue_title": self.issue.title, "issue_description": self.issue.description}, config
                    )
                except Exception as e:
                    raise UnableToPlanIssueError("Error planning issue") from e
                else:
                    if result.get("request_for_changes") is False or INTERRUPT not in result:
                        raise UnableToPlanIssueError("No plan was generated.")

                    # The first task is the plan_and_execute node
                    self._handle_initial_result(result[INTERRUPT][0].value)

            # if the agent is waiting for the human to approve the plan on the sub-graph of the plan_and_execute node,
            # we extract the note left by the human and resume the execution of the plan_and_execute node
            elif (
                "plan_and_execute" in current_state.next
                and "plan_approval" in current_state.tasks[0].state.next
                and (discussions := self.client.get_issue_discussions(self.repo_id, cast("int", self.issue.iid)))
            ):
                # TODO: Improve discovery of the last discussion awaiting for approval
                # Skip first note because it's the bot note
                approval_messages = notes_to_messages(discussions[-1].notes[1:], self.client.current_user.id)

                try:
                    for _ns, payload in issue_addressor.agent.stream(
                        Command(resume=approval_messages), config, stream_mode="updates", subgraphs=True
                    ):
                        if "plan_approval" in payload and (
                            response := payload["plan_approval"].get("plan_approval_response")
                        ):
                            self.client.create_issue_discussion_note(
                                self.repo_id, cast("int", self.issue.iid), response, discussion_id=discussions[-1].id
                            )
                except Exception as e:
                    raise UnableToExecutePlanError("Error executing plan") from e

            elif current_state.tasks:
                # This can happen if the agent got an error and we need to retry, or was interrupted.
                issue_addressor.agent.invoke(None, config)

            if file_changes := self._get_file_changes():
                # when changes where made by the agent, commit them
                self._commit_changes(file_changes=file_changes, thread_id=self.thread_id)

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

    def _handle_initial_result(self, state: dict):
        """
        Handle the initial state of issue processing.

        Args:
            state: The state of the agent.
        """
        # All good, we share the plan with the human to review and approve
        if plan_tasks := state.get("plan_tasks"):
            self.client.comment_issue(
                self.repo_id,
                cast("int", self.issue.iid),
                jinja2_formatter(ISSUE_REVIEW_PLAN_TEMPLATE, plan_tasks=plan_tasks),
            )
        # We share the questions with the human to answer
        elif plan_questions := state.get("plan_questions"):
            self.client.comment_issue(
                self.repo_id,
                cast("int", self.issue.iid),
                jinja2_formatter(ISSUE_QUESTIONS_TEMPLATE, questions=plan_questions),
            )
        else:
            raise UnableToPlanIssueError("Unexpected state from plan and execute node")

    @override
    def _commit_changes(self, *, file_changes: list[FileChange], thread_id: str | None = None, skip_ci: bool = False):
        """
        Process file changes and create or update merge request.

        Args:
            file_changes: The file changes to commit.
            thread_id: The thread ID.
            skip_ci: Whether to skip the CI.
        """
        pr_describer = PullRequestDescriberAgent()
        changes_description = pr_describer.agent.invoke(
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
            config={
                "tags": [pr_describer_settings.NAME, str(self.client.client_slug)],
                "configurable": {"thread_id": thread_id},
            },
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

        merge_request_id = self.client.update_or_create_merge_request(
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
            ),
        )

        self.client.comment_issue(
            self.repo_id,
            cast("int", self.issue.iid),
            ISSUE_PROCESSED_TEMPLATE.format(source_repo_id=self.repo_id, merge_request_id=merge_request_id),
        )
