import logging
from typing import TYPE_CHECKING

from django.conf import settings as django_settings
from django.template.loader import render_to_string

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from automation.agent.graph import create_daiv_agent
from automation.agent.utils import extract_text_content, get_daiv_agent_kwargs
from codebase.base import GitPlatform, Issue
from core.utils import generate_uuid

from .base import BaseManager

if TYPE_CHECKING:
    from codebase.context import RuntimeCtx

logger = logging.getLogger("daiv.managers")


PLAN_ISSUE_PROMPT = "Present a plan to address this issue and wait for approval before executing it."
ADDRESS_ISSUE_PROMPT = "Address this issue."


class IssueAddressorManager(BaseManager):
    """
    Manages the issue processing and addressing workflow.
    """

    def __init__(self, *, issue: Issue, mention_comment_id: str | None = None, runtime_ctx: RuntimeCtx):
        super().__init__(runtime_ctx=runtime_ctx)
        self.issue = issue
        self.thread_id = generate_uuid(f"{self.ctx.repo_id}:{issue.iid}")
        self.mention_comment_id = mention_comment_id

    @classmethod
    async def address_issue(cls, *, issue: Issue, mention_comment_id: str | None = None, runtime_ctx: RuntimeCtx):
        """
        Address the issue.

        Args:
            issue (Issue): The issue object.
            mention_comment_id (str | None): The mention comment id. Defaults to None.
            runtime_ctx (RuntimeCtx): The runtime context.
        """
        manager = cls(issue=issue, mention_comment_id=mention_comment_id, runtime_ctx=runtime_ctx)

        try:
            await manager._address_issue()
        except Exception as e:
            logger.exception("Error addressing issue %d: %s", issue.iid, e)
            manager._add_unable_to_address_issue_note()

    async def _address_issue(self):
        """
        Process the issue by addressing it with the appropriate actions.
        """
        messages = []

        if self.mention_comment_id:
            # The issue was triggered by a mention in a comment, so we need to add the comment to the messages.
            mention_comment = self.client.get_issue_comment(self.ctx.repo_id, self.issue.iid, self.mention_comment_id)
            messages.append(
                HumanMessage(
                    name=mention_comment.notes[0].author.username,
                    id=mention_comment.notes[0].id,
                    content=mention_comment.notes[0].body,
                )
            )
        else:
            # The issue was triggered by the bot label, so we need to request the agent to address it.
            message_content = ADDRESS_ISSUE_PROMPT if self.issue.has_auto_label() else PLAN_ISSUE_PROMPT
            messages.append(HumanMessage(name=self.issue.author.username, id=self.issue.id, content=message_content))

        async with AsyncPostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
            daiv_agent = await create_daiv_agent(
                ctx=self.ctx,
                checkpointer=checkpointer,
                store=self.store,
                **get_daiv_agent_kwargs(model_config=self.ctx.config.models.agent, use_max=self.issue.has_max_label()),
            )

            result = await daiv_agent.ainvoke(
                {"messages": messages},
                config=RunnableConfig(
                    tags=[daiv_agent.get_name(), self.client.git_platform.value],
                    metadata={
                        "author": self.issue.author.username,
                        "issue_id": self.issue.iid,
                        "scope": self.ctx.scope,
                        "use_max_model": self.issue.has_max_label(),
                    },
                    configurable={"thread_id": self.thread_id},
                ),
                context=self.ctx,
            )

            response = result and extract_text_content(result["messages"][-1].content)

            if merge_request_id := result.get("merge_request_id"):
                self._add_issue_addressed_note(merge_request_id, response)
            else:
                self._create_or_update_comment(response)

    def _add_unable_to_address_issue_note(self):
        """
        Add a note to the issue to inform the user that the response could not be generated.
        """
        self._create_or_update_comment(
            render_to_string("codebase/issue_unable_address_issue.txt", {"bot_username": self.ctx.bot_username}),
            reply_to_id=self.mention_comment_id,
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

    def _create_or_update_comment(self, note_message: str, reply_to_id: str | None = None):
        """
        Create or update a comment on the issue.

        Args:
            note_message: The message to add to the comment.
            reply_to_id: The ID of the comment to reply to.
        """
        if self._comment_id is not None:
            self.client.update_issue_comment(
                self.ctx.repo_id, self.issue.iid, self._comment_id, note_message, reply_to_id=reply_to_id
            )
        else:
            self._comment_id = self.client.create_issue_comment(
                self.ctx.repo_id, self.issue.iid, note_message, reply_to_id=reply_to_id
            )
