import logging
from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from langchain_core.messages import HumanMessage

from automation.agent.graph import create_daiv_agent
from automation.agent.usage_tracking import build_usage_summary, track_usage_metadata
from automation.agent.utils import build_langsmith_config, extract_text_content, get_daiv_agent_kwargs
from codebase.base import GitPlatform, Scope
from codebase.utils import compute_thread_id
from core.checkpointer import open_checkpointer
from core.constants import BOT_NAME

from .base import BaseManager

if TYPE_CHECKING:
    from automation.agent.results import AgentResult
    from codebase.base import Issue
    from codebase.context import RuntimeCtx

logger = logging.getLogger("daiv.managers")


PLAN_ISSUE_PROMPT = "/plan address the issue #{issue_iid}"
ADDRESS_ISSUE_PROMPT = "Address the issue #{issue_iid}."


class IssueAddressorManager(BaseManager):
    """
    Manages the issue processing and addressing workflow.
    """

    def __init__(
        self,
        *,
        issue: Issue,
        mention_comment_id: str | None = None,
        runtime_ctx: RuntimeCtx,
        thread_id: str | None = None,
    ):
        super().__init__(runtime_ctx=runtime_ctx)
        self.issue = issue
        if thread_id is None:
            thread_id = compute_thread_id(repo_slug=self.ctx.repository.slug, scope=Scope.ISSUE, entity_iid=issue.iid)
        elif not thread_id:
            raise ValueError(f"thread_id must be non-empty or None, got {thread_id!r}")
        self.thread_id = thread_id
        self.mention_comment_id = mention_comment_id

    @classmethod
    async def address_issue(
        cls,
        *,
        issue: Issue,
        mention_comment_id: str | None = None,
        runtime_ctx: RuntimeCtx,
        thread_id: str | None = None,
    ) -> AgentResult:
        """
        Address the issue.

        Args:
            issue (Issue): The issue object.
            mention_comment_id (str | None): The mention comment id. Defaults to None.
            runtime_ctx (RuntimeCtx): The runtime context.

        Returns:
            An :class:`AgentResult` dict with the agent response and code_changes flag.
        """
        manager = cls(issue=issue, mention_comment_id=mention_comment_id, runtime_ctx=runtime_ctx, thread_id=thread_id)

        try:
            return await manager._address_issue()
        except Exception:
            manager._add_unable_to_address_issue_note()
            raise

    async def _address_issue(self) -> AgentResult:
        """
        Process the issue by addressing it with the appropriate actions.
        """
        messages = []
        triggered_by = self.issue.author.username

        if self.mention_comment_id:
            # The issue was triggered by a mention in a comment, so we need to add the comment to the messages.
            mention_comment = self.client.get_issue_comment(
                self.ctx.repository.slug, self.issue.iid, self.mention_comment_id
            )
            latest_comment = mention_comment.notes[-1]
            triggered_by = latest_comment.author.username
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

        async with open_checkpointer() as checkpointer:
            agent_kwargs = get_daiv_agent_kwargs(
                model_config=self.ctx.config.models.agent, use_max=self.issue.has_max_label()
            )
            daiv_agent = await create_daiv_agent(
                ctx=self.ctx, checkpointer=checkpointer, store=self.store, **agent_kwargs
            )
            agent_config = build_langsmith_config(
                self.ctx,
                trigger="mention" if self.mention_comment_id else "label",
                model=agent_kwargs["model_names"][0],
                thinking_level=agent_kwargs["thinking_level"],
                agent_name=daiv_agent.get_name(),
                configurable={"thread_id": self.thread_id},
                extra_metadata={
                    "author": self.issue.author.username,
                    "triggered_by": triggered_by,
                    "issue_id": self.issue.iid,
                    "labels": [label.lower() for label in self.issue.labels],
                },
            )
            try:
                with track_usage_metadata() as usage_handler:
                    result = await daiv_agent.ainvoke({"messages": messages}, config=agent_config, context=self.ctx)
            except Exception:
                draft_published = await self._recover_draft(
                    daiv_agent, agent_config, entity_label="issue", entity_id=self.issue.iid
                )
                self._add_unable_to_address_issue_note(draft_published=draft_published)
                raise
            else:
                response_text = ""
                if (
                    result
                    and "messages" in result
                    and result["messages"]
                    and (response_text := extract_text_content(result["messages"][-1].content).strip())
                ):
                    self._leave_comment(response_text)
                else:
                    logger.warning(
                        "Agent returned empty response for issue %d (result keys: %s)",
                        self.issue.iid,
                        list(result.keys()) if result else None,
                    )
                    self._add_unable_to_address_issue_note()

                return await self._build_agent_result(
                    daiv_agent, agent_config, response=response_text, usage=build_usage_summary(usage_handler).to_dict()
                )

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
                    "is_github": self.ctx.git_platform == GitPlatform.GITHUB,
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
        return self.client.create_issue_comment(self.ctx.repository.slug, self.issue.iid, body, reply_to_id=reply_to_id)
