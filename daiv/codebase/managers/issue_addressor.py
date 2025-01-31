import logging
from typing import cast

from django.conf import settings

from langchain_core.prompts.string import jinja2_formatter
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import START

from automation.agents.issue_addressor.agent import IssueAddressorAgent
from automation.agents.issue_addressor.state import OverallState
from automation.agents.issue_addressor.templates import (
    ISSUE_MERGE_REQUEST_TEMPLATE,
    ISSUE_PLANNING_TEMPLATE,
    ISSUE_PROCESSED_TEMPLATE,
    ISSUE_QUESTIONS_TEMPLATE,
    ISSUE_REPLAN_TEMPLATE,
    ISSUE_REVIEW_PLAN_TEMPLATE,
    ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE,
)
from automation.agents.pr_describer.agent import PullRequestDescriberAgent
from codebase.base import FileChange, Issue, Note
from codebase.clients import AllRepoClient, RepoClient
from codebase.managers.base import BaseManager
from codebase.utils import notes_to_messages
from core.constants import BOT_LABEL, BOT_NAME
from core.utils import generate_uuid

logger = logging.getLogger("daiv.managers")


class IssueAddressorManager(BaseManager):
    """
    Manages the issue processing and addressing workflow.
    """

    def __init__(self, client: AllRepoClient, repo_id: str, ref: str | None = None):
        super().__init__(client, repo_id, ref)
        self.repository = self.client.get_repository(repo_id)

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
        manager = cls(client, repo_id, ref)
        try:
            manager._process_issue(client.get_issue(repo_id, issue_iid), should_reset_plan)
        except Exception as e:
            logger.exception("Error processing issue %d: %s", issue_iid, e)
            client.comment_issue(repo_id, issue_iid, ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE)

    def _process_issue(self, issue: Issue, should_reset_plan: bool):
        """
        Process the issue by addressing it with the appropriate actions.

        Args:
            issue: The issue to process.
            should_reset_plan: Whether to reset the plan.
        """
        # Initialize issue if no bot comment exists
        if not self._has_bot_notes(issue.notes):
            self.client.comment_issue(
                self.repo_id,
                cast("int", issue.iid),
                jinja2_formatter(
                    ISSUE_PLANNING_TEMPLATE,
                    assignee=issue.assignee.username if issue.assignee else None,
                    bot_name=BOT_NAME,
                ),
            )

        config = RunnableConfig(configurable={"thread_id": generate_uuid(f"{self.repo_id}{issue.iid}")})

        with PostgresSaver.from_conn_string(settings.DB_URI) as checkpointer:
            issue_addressor = IssueAddressorAgent(
                self.client,
                project_id=self.repository.pk,
                source_repo_id=self.repo_id,
                source_ref=self.ref,
                issue_id=cast("int", issue.iid),
                checkpointer=checkpointer,
            )
            issue_addressor_agent = issue_addressor.agent

            if should_reset_plan and (
                history_states := list(issue_addressor_agent.get_state_history(config, filter={"step": -1}))
            ):
                config = history_states[-1].config
                self.client.comment_issue(self.repo_id, cast("int", issue.iid), ISSUE_REPLAN_TEMPLATE)

            current_state = issue_addressor_agent.get_state(config)

            if (not current_state.next and current_state.created_at is None) or START in current_state.next:
                result = issue_addressor_agent.invoke(
                    {"issue_title": issue.title, "issue_description": issue.description}, config
                )

                self._handle_initial_result(result, cast("int", issue.iid))

            elif "human_feedback" in current_state.next and (
                discussions := self.client.get_issue_discussions(self.repo_id, cast("int", issue.iid))
            ):
                # TODO: Improve discovery of the last discussion awaiting for approval
                issue_addressor_agent.update_state(
                    config,
                    # Skip first note because it's the bot note
                    {"messages": notes_to_messages(discussions[-1].notes[1:], self.client.current_user.id)},
                )

                for chunk in issue_addressor_agent.stream(None, config, stream_mode="updates"):
                    if "human_feedback" in chunk and (response := chunk["human_feedback"].get("response")):
                        self.client.create_issue_discussion_note(
                            self.repo_id, cast("int", issue.iid), response, discussion_id=discussions[-1].id
                        )

            elif current_state.tasks:
                # This can happen if the agent got an error and we need to retry, or was interrupted.
                result = issue_addressor_agent.invoke(None, config)

            # when changes where made by the agent, commit them
            if file_changes := issue_addressor.get_files_to_commit():
                self._commit_changes(issue, file_changes)

    def _has_bot_notes(self, notes: list[Note]) -> bool:
        """
        Check if the issue already has a comment from the bot.

        Args:
            notes: The notes to check.

        Returns:
            True if the issue has a comment from the bot, otherwise False.
        """
        return any(note.author.id == self.client.current_user.id for note in notes)

    def _handle_initial_result(self, result: OverallState, issue_iid: int):
        """
        Handle the initial state of issue processing.

        Args:
            result: The result of the issue processing.
            issue_iid: The issue ID.
        """
        if "plan_tasks" in result and result["plan_tasks"]:
            self.client.comment_issue(
                self.repo_id, issue_iid, jinja2_formatter(ISSUE_REVIEW_PLAN_TEMPLATE, plan_tasks=result["plan_tasks"])
            )
        elif "questions" in result and result["questions"]:
            self.client.comment_issue(
                self.repo_id, issue_iid, jinja2_formatter(ISSUE_QUESTIONS_TEMPLATE, questions=result["questions"])
            )
        else:
            self.client.comment_issue(self.repo_id, issue_iid, ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE)

    def _commit_changes(self, issue: Issue, file_changes: list[FileChange]):
        """
        Process file changes and create or update merge request.

        Args:
            issue: The issue to process.
            file_changes: The file changes to commit.
        """
        pr_describer = PullRequestDescriberAgent()
        changes_description = pr_describer.agent.invoke({
            "changes": file_changes,
            "extra_details": {"Issue title": issue.title, "Issue description": cast("str", issue.description)},
            "branch_name_convention": self.repo_config.branch_name_convention,
        })

        merge_requests = self.client.get_issue_related_merge_requests(
            self.repo_id, cast("int", issue.iid), label=BOT_LABEL
        )

        if merge_requests:
            changes_description.branch = merge_requests[0].source_branch
        else:
            changes_description.branch = self._get_unique_branch_name(changes_description.branch)

        self.client.commit_changes(
            self.repo_id,
            changes_description.branch,
            changes_description.commit_message,
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
            assignee_id=issue.assignee.id if issue.assignee else None,
            description=jinja2_formatter(
                ISSUE_MERGE_REQUEST_TEMPLATE,
                description=changes_description.description,
                summary=changes_description.summary,
                source_repo_id=self.repo_id,
                issue_id=issue.iid,
                bot_name=BOT_NAME,
            ),
        )

        self.client.comment_issue(
            self.repo_id,
            cast("int", issue.iid),
            ISSUE_PROCESSED_TEMPLATE.format(source_repo_id=self.repo_id, merge_request_id=merge_request_id),
        )
