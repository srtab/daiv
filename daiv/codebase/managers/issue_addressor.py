import logging
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING, cast, override

from django.conf import settings as django_settings
from django.template.loader import render_to_string

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import merge_configs
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from automation.agents.deepagent.graph import create_daiv_agent
from automation.agents.deepagent.utils import get_daiv_agent_kwargs
from automation.agents.pr_describer import PullRequestDescriberAgent
from automation.agents.pr_describer.conf import settings as pr_describer_settings
from automation.agents.utils import extract_text_content, get_context_file_content
from codebase.base import GitPlatform, Issue
from codebase.utils import redact_diff_content
from core.constants import BOT_LABEL, BOT_NAME
from core.utils import generate_uuid

from .base import BaseManager

if TYPE_CHECKING:
    from codebase.context import RuntimeCtx

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


class IssueAddressorManager(BaseManager):
    """
    Manages the issue processing and addressing workflow.
    """

    def __init__(self, *, issue_iid: int, mention_comment_id: str, runtime_ctx: RuntimeCtx):
        super().__init__(runtime_ctx=runtime_ctx)
        self.issue: Issue = self.client.get_issue(self.ctx.repo_id, issue_iid)
        self.thread_id = generate_uuid(f"{self.ctx.repo_id}:{issue_iid}:1")
        self.mention_comment_id = mention_comment_id

    @classmethod
    async def address_issue(cls, *, issue_iid: int, mention_comment_id: str, runtime_ctx: RuntimeCtx):
        """
        Address the issue.

        Args:
            issue_iid (int): The issue ID.
            mention_comment_id (str): The mention comment id.
            runtime_ctx (RuntimeCtx): The runtime context.
        """
        manager = cls(issue_iid=issue_iid, mention_comment_id=mention_comment_id, runtime_ctx=runtime_ctx)

        try:
            await manager._address_issue()
        except Exception as e:
            logger.exception("Error addressing issue %d: %s", issue_iid, e)
            manager._add_unable_to_address_issue_note()

    async def _address_issue(self):
        """
        Process the issue by addressing it with the appropriate actions.
        """
        config = self._config
        mention_comment = self.client.get_issue_comment(self.ctx.repo_id, self.issue.iid, self.mention_comment_id)

        async with AsyncPostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
            daiv_agent = await create_daiv_agent(
                ctx=self.ctx,
                checkpointer=checkpointer,
                store=self.store,
                issue_id=self.issue.iid,
                **get_daiv_agent_kwargs(model_config=self.ctx.config.models.daiv, use_max=self.issue.has_max_label()),
            )

            result = await daiv_agent.ainvoke(
                {
                    "messages": [
                        HumanMessage(
                            name=mention_comment.notes[0].author.username,
                            id=mention_comment.notes[0].id,
                            content=mention_comment.notes[0].body,
                        )
                    ]
                },
                merge_configs(config, RunnableConfig(tags=[daiv_agent.name])),
                context=self.ctx,
            )

            response = result and extract_text_content(result["messages"][-1].content)

            if self.git_manager.is_dirty() and (
                merge_request_id := await self._commit_changes(thread_id=self.thread_id)
            ):
                self._add_issue_addressed_note(merge_request_id, response)
            else:
                self._create_or_update_comment(response)

    @property
    def _config(self):
        """
        Get the config for the agent.
        """
        return RunnableConfig(
            tags=[str(self.client.git_platform)],
            metadata={"author": self.issue.author.username, "issue_id": self.issue.iid},
            configurable={"thread_id": self.thread_id},
        )

    @override
    async def _commit_changes(self, *, thread_id: str | None = None, skip_ci: bool = False) -> int | str | None:
        """
        Process file changes and create or update merge request.

        Args:
            thread_id: The thread ID.
            skip_ci: Whether to skip the CI.
        """
        pr_describer = await PullRequestDescriberAgent.get_runnable(model=self.ctx.config.models.pr_describer.model)
        changes_description = await pr_describer.ainvoke(
            {
                "diff": redact_diff_content(self.git_manager.get_diff(), self.ctx.config.omit_content_patterns),
                "context_file_content": get_context_file_content(
                    Path(self.ctx.repo.working_dir), self.ctx.config.context_file_name
                ),
                "extra_context": dedent(
                    """\
                    This changes were made to address the following issue:

                    Issue title: {title}
                    Issue description: {description}
                    """
                ).format(title=self.issue.title, description=self.issue.description),
            },
            config=RunnableConfig(
                tags=[pr_describer_settings.NAME, str(self.client.git_platform)], configurable={"thread_id": thread_id}
            ),
        )
        merge_requests = self.client.get_issue_related_merge_requests(
            self.ctx.repo_id, cast("int", self.issue.iid), label=BOT_LABEL
        )

        if merge_requests:
            changes_description.branch = merge_requests[0].source_branch

        branch_name = self.git_manager.commit_changes(
            changes_description.commit_message,
            branch_name=changes_description.branch,
            skip_ci=skip_ci,
            override_commits=True,
            use_branch_if_exists=bool(merge_requests),
        )

        if self.issue.assignee:
            assignee_id = (
                self.issue.assignee.id
                if self.client.git_platform == GitPlatform.GITLAB
                else self.issue.assignee.username
            )
        else:
            assignee_id = None

        return self.client.update_or_create_merge_request(
            repo_id=self.ctx.repo_id,
            source_branch=branch_name,
            target_branch=self.ctx.config.default_branch,
            labels=[BOT_LABEL],
            title=changes_description.title,
            assignee_id=assignee_id,
            description=render_to_string(
                "codebase/issue_merge_request.txt",
                {
                    "description": changes_description.description,
                    "source_repo_id": self.ctx.repo_id,
                    "issue_id": self.issue.iid,
                    "bot_name": BOT_NAME,
                    "bot_username": self.ctx.bot_username,
                    "is_gitlab": self.client.git_platform == GitPlatform.GITLAB,
                },
            ),
        )

    def _add_unable_to_address_issue_note(self):
        """
        Add a note to the issue to inform the user that the issue could not be addressed.
        """

        self._create_or_update_comment(
            render_to_string("codebase/issue_unable_address_issue.txt", {"bot_name": BOT_NAME})
        )

    def _add_issue_addressed_note(self, merge_request_id: int, message: str):
        """
        Add a note to the issue to inform the user that the issue has been addressed.
        """
        self._create_or_update_comment(
            render_to_string(
                "codebase/issue_addressed.txt",
                {
                    "source_repo_id": self.ctx.repo_id,
                    "merge_request_id": merge_request_id,
                    # GitHub already shows the merge request link right after the comment.
                    "show_merge_request_link": self.client.git_platform == GitPlatform.GITLAB,
                    "message": message,
                },
            )
        )

    def _create_or_update_comment(self, note_message: str):
        """
        Create or update a comment on the issue.
        """
        if self._comment_id is not None:
            self.client.update_issue_comment(self.ctx.repo_id, self.issue.iid, self._comment_id, note_message)
        else:
            self._comment_id = self.client.create_issue_comment(self.ctx.repo_id, self.issue.iid, note_message)
