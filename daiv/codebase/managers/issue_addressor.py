import logging
from typing import TYPE_CHECKING

from django.conf import settings as django_settings
from django.template.loader import render_to_string

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from automation.agent.graph import create_daiv_agent
from automation.agent.publishers import GitChangePublisher
from automation.agent.utils import extract_text_content, get_daiv_agent_kwargs
from codebase.base import GitPlatform
from core.constants import BOT_NAME
from core.utils import generate_uuid

from .base import BaseManager

if TYPE_CHECKING:
    from codebase.base import Issue
    from codebase.context import RuntimeCtx

logger = logging.getLogger("daiv.managers")


PLAN_ISSUE_PROMPT = "/plan address the issue #{issue_iid}"
ADDRESS_ISSUE_PROMPT = "Address the issue #{issue_iid}."


class IssueAddressorManager(BaseManager):
    """
    Manages the issue processing and addressing workflow.
    """

    def __init__(self, *, issue: Issue, mention_comment_id: str | None = None, runtime_ctx: RuntimeCtx):
        super().__init__(runtime_ctx=runtime_ctx)
        self.issue = issue
        self.thread_id = generate_uuid(f"{self.ctx.repo_id}:{self.ctx.scope}/{issue.iid}")
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
            latest_comment = mention_comment.notes[-1]
            messages.append(
                HumanMessage(name=latest_comment.author.username, id=latest_comment.id, content=latest_comment.body)
            )
        else:
            # The issue was triggered by the bot label, so we need to request the agent to address it.
            message_content = (
                ADDRESS_ISSUE_PROMPT.format(issue_iid=self.issue.iid)
                if self.issue.has_auto_label()
                else PLAN_ISSUE_PROMPT.format(issue_iid=self.issue.iid)
            )
            messages.append(
                HumanMessage(name=self.issue.author.username, id=str(self.issue.iid), content=message_content)
            )

        async with AsyncPostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
            daiv_agent = await create_daiv_agent(
                ctx=self.ctx,
                checkpointer=checkpointer,
                store=self.store,
                **get_daiv_agent_kwargs(model_config=self.ctx.config.models.agent, use_max=self.issue.has_max_label()),
            )
            agent_config = RunnableConfig(
                configurable={"thread_id": self.thread_id},
                tags=[daiv_agent.get_name(), self.client.git_platform.value],
                metadata={
                    "author": self.issue.author.username,
                    "issue_id": self.issue.iid,
                    "labels": [label.lower() for label in self.issue.labels],
                    "scope": self.ctx.scope,
                },
            )
            try:
                result = await daiv_agent.ainvoke({"messages": messages}, config=agent_config, context=self.ctx)
            except Exception:
                snapshot = await daiv_agent.aget_state(config=agent_config)

                # If and unexpect error occurs while addressing the issue, a draft merge request is created to avoid
                # losing the changes made by the agent.
                merge_request = snapshot.values.get("merge_request")
                publisher = GitChangePublisher(self.ctx)
                merge_request = await publisher.publish(
                    merge_request=merge_request, as_draft=(merge_request is None or merge_request.draft)
                )

                # If the draft merge request is created successfully, we update the state to reflect the new MR.
                if merge_request:
                    await daiv_agent.aupdate_state(config=agent_config, values={"merge_request": merge_request})

                self._add_unable_to_address_issue_note(draft_published=bool(merge_request))
            else:
                if (
                    result
                    and "messages" in result
                    and result["messages"]
                    and (response_text := extract_text_content(result["messages"][-1].content).strip())
                ):
                    self._leave_comment(response_text)
                else:
                    self._add_unable_to_address_issue_note()

    def _add_unable_to_address_issue_note(self, *, draft_published: bool = False):
        """
        Add a note to the issue to inform the user that the response could not be generated.
        """
        self._leave_comment(
            render_to_string(
                "codebase/unable_address_issue.txt",
                {
                    "bot_name": BOT_NAME,
                    "bot_username": self.ctx.bot_username,
                    "draft_published": draft_published,
                    "is_gitlab": self.ctx.git_platform == GitPlatform.GITLAB,
                },
            ),
            # GitHub doesn't support replying to comments, so we need to provide a reply_to_id only for GitLab.
            reply_to_id=self.mention_comment_id if self.ctx.git_platform == GitPlatform.GITLAB else None,
        )

    def _leave_comment(self, body: str, reply_to_id: str | None = None):
        """
        Leave a comment on the issue.

        Args:
            body: The body of the comment.
            reply_to_id: The ID of the comment to reply to. This is not supported for GitHub.
        """
        return self.client.create_issue_comment(self.ctx.repo_id, self.issue.iid, body, reply_to_id=reply_to_id)
