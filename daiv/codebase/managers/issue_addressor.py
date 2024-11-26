from typing import cast

from django.conf import settings

from langchain_community.callbacks import get_openai_callback
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
    ISSUE_REVIEW_PLAN_TEMPLATE,
    ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE,
)
from automation.agents.pr_describer.agent import PullRequestDescriberAgent
from automation.agents.schemas import Task
from codebase.base import FileChange, Issue, IssueType, Note
from codebase.clients import AllRepoClient, RepoClient
from codebase.utils import notes_to_messages
from core.config import RepositoryConfig
from core.constants import BOT_LABEL, BOT_NAME


class IssueAddressorManager:
    """
    Manages the issue processing and addressing workflow.
    """

    def __init__(self, client: AllRepoClient, repo_id: str, ref: str | None = None):
        self.client = client
        self.repo_id = repo_id

        self.repository = self.client.get_repository(repo_id)
        self.repo_config = RepositoryConfig.get_config(repo_id=repo_id, repository=self.repository)

        self.ref = cast(str, ref or self.repo_config.default_branch)

    @classmethod
    def process_issue(cls, repo_id: str, issue_iid: int, ref: str | None = None, should_reset_plan: bool = False):
        """
        Process an issue by creating a merge request with the changes described in the issue description.
        """
        client = RepoClient.create_instance()
        manager = cls(client, repo_id, ref)
        manager._process_issue(client.get_issue(repo_id, issue_iid), should_reset_plan)

    def _process_issue(self, issue: Issue, should_reset_plan: bool):
        """
        Process the issue by addressing it with the appropriate actions.
        """
        # Initialize issue if no bot comment exists
        if not self._has_bot_notes(issue.notes):
            self.client.comment_issue(
                self.repo_id,
                cast(int, issue.iid),
                jinja2_formatter(
                    ISSUE_PLANNING_TEMPLATE,
                    assignee=issue.assignee.username if issue.assignee else None,
                    bot_name=BOT_NAME,
                ),
            )

        config = RunnableConfig(configurable={"thread_id": f"{self.repo_id}#{issue.iid}"})

        with PostgresSaver.from_conn_string(settings.DB_URI) as checkpointer, get_openai_callback() as usage_handler:
            issue_addressor = IssueAddressorAgent(
                self.client,
                project_id=self.repository.pk,
                source_repo_id=self.repo_id,
                source_ref=self.ref,
                issue_id=cast(int, issue.iid),
                checkpointer=checkpointer,
                usage_handler=usage_handler,
            )
            issue_addressor_agent = issue_addressor.agent

            if should_reset_plan and (
                history_states := list(issue_addressor_agent.get_state_history(config, filter={"step": -1}))
            ):
                config = history_states[-1].config

            current_state = issue_addressor_agent.get_state(config)

            if (not current_state.next and current_state.created_at is None) or START in current_state.next:
                result = issue_addressor_agent.invoke(
                    {"issue_title": issue.title, "issue_description": issue.description}, config
                )

                self._handle_initial_result(result, cast(int, issue.id), cast(int, issue.iid))

            elif "human_feedback" in current_state.next and (
                discussions := self.client.get_issue_discussions(self.repo_id, cast(int, issue.iid))
            ):
                # TODO: Improve discovery of the last discussion awaiting for approval
                issue_addressor_agent.update_state(
                    config, {"messages": notes_to_messages(discussions[-1].notes, self.client.current_user.id)}
                )

                for chunk in issue_addressor_agent.stream(None, config, stream_mode="updates"):
                    if "human_feedback" in chunk and (response := chunk["human_feedback"].get("response")):
                        self.client.create_issue_discussion_note(
                            self.repo_id, cast(int, issue.iid), response, discussion_id=discussions[-1].id
                        )

                    if "execute_plan" in chunk and (file_changes := issue_addressor.get_files_to_commit()):
                        self._commit_changes(issue, file_changes)

            elif current_state.tasks:
                # This can happen if the agent got an error and we need to retry, or was interrupted.
                result = issue_addressor_agent.invoke(None, config)

    def _has_bot_notes(self, notes: list[Note]) -> bool:
        """
        Check if the issue already has a comment from the bot.
        """
        return any(note.author.id == self.client.current_user.id for note in notes)

    def _handle_initial_result(self, result: OverallState, issue_id: int, issue_iid: int):
        """
        Handle the initial state of issue processing.
        """
        if "plan_tasks" in result:
            # clean up existing tasks before creating new ones
            for issue_tasks in self.client.get_issue_tasks(self.repo_id, issue_id):
                self.client.delete_issue(self.repo_id, cast(int, issue_tasks.iid))

            # create new tasks and comment the issue
            self.client.create_issue_tasks(self.repo_id, issue_id, self._create_issue_tasks(result["plan_tasks"]))
            self.client.comment_issue(self.repo_id, issue_iid, ISSUE_REVIEW_PLAN_TEMPLATE)
        elif "questions" in result:
            self.client.comment_issue(self.repo_id, issue_iid, "\n".join(result["questions"]))
        else:
            self.client.comment_issue(self.repo_id, issue_iid, ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE)

    def _create_issue_tasks(self, plan_tasks: list[Task]) -> list[Issue]:
        """
        Create issue task objects from plan tasks.
        """
        return [
            Issue(
                title=plan_task.title,
                description="{context}\n{subtasks}\n\nPath: `{path}`".format(
                    context=plan_task.context, subtasks="\n - [ ] ".join(plan_task.subtasks), path=plan_task.path
                ),
                assignee=self.client.current_user,
                issue_type=IssueType.TASK,
                labels=[BOT_LABEL],
            )
            for plan_task in plan_tasks
        ]

    def _commit_changes(self, issue: Issue, file_changes: list[FileChange]):
        """
        Process file changes and create or update merge request.
        """

        pr_describer = PullRequestDescriberAgent()
        changes_description = pr_describer.agent.invoke({
            "changes": file_changes,
            "extra_details": {"Issue title": issue.title, "Issue description": cast(str, issue.description)},
            "branch_name_convention": self.repo_config.branch_name_convention,
        })

        merge_requests = self.client.get_issue_related_merge_requests(
            self.repo_id, cast(int, issue.iid), label=BOT_LABEL
        )

        if merge_requests:
            changes_description.branch = merge_requests[0].source_branch

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
            cast(int, issue.iid),
            ISSUE_PROCESSED_TEMPLATE.format(source_repo_id=self.repo_id, merge_request_id=merge_request_id),
        )
