import logging
from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from langchain_core.messages import HumanMessage
from sessions.executor.run import execute_run
from sessions.executor.spec import RunHooks, RunSpec

from automation.agent.validators import AgentConfigurationError
from codebase.base import GitPlatform, Scope
from codebase.utils import resolve_thread_id
from core.constants import BOT_NAME

from .base import BaseManager

if TYPE_CHECKING:
    from langgraph.types import StateSnapshot
    from sessions.executor.spec import RunOutcome

    from automation.agent.results import AgentResult
    from codebase.base import Issue

logger = logging.getLogger("daiv.managers")


PLAN_ISSUE_PROMPT = "/plan address the issue #{issue_iid}"
ADDRESS_ISSUE_PROMPT = "Address the issue #{issue_iid}."


class IssueAddressorManager(BaseManager):
    """
    Runs the agent on an issue and answers on it.
    """

    def __init__(
        self, *, repo_id: str, issue: Issue, mention_comment_id: str | None = None, thread_id: str | None = None
    ):
        super().__init__(
            repo_id=repo_id,
            thread_id=resolve_thread_id(thread_id, repo_slug=repo_id, scope=Scope.ISSUE, entity_iid=issue.iid),
            mention_comment_id=mention_comment_id,
        )
        self.issue = issue

    @classmethod
    async def address_issue(
        cls,
        *,
        repo_id: str,
        issue: Issue,
        mention_comment_id: str | None = None,
        ref: str | None = None,
        thread_id: str | None = None,
        sandbox_env_id: str | None = None,
    ) -> AgentResult | None:
        """
        Address the issue.

        Args:
            repo_id: The repository slug.
            issue: The issue object.
            mention_comment_id: The discussion that mentioned the bot, or ``None`` for a label trigger.
            ref: The branch to clone; ``None`` for the repository default.
            thread_id: The session's thread id; ``None`` computes the deterministic one.
            sandbox_env_id: The sandbox environment the callback selected.

        Returns:
            An :class:`AgentResult`, or ``None`` when no model is configured (after saying so on the issue).
        """
        manager = cls(repo_id=repo_id, issue=issue, mention_comment_id=mention_comment_id, thread_id=thread_id)

        try:
            return await manager._address_issue(ref=ref, sandbox_env_id=sandbox_env_id)
        except AgentConfigurationError:
            return None
        except Exception:
            manager._add_unable_to_address_issue_note()
            raise

    async def _address_issue(self, *, ref: str | None, sandbox_env_id: str | None) -> AgentResult:
        message, triggered_by = self._input_message()
        outcome = await execute_run(
            RunSpec(
                thread_id=self.thread_id,
                repo_id=self.repo_id,
                scope=Scope.ISSUE,
                input_messages=(message,),
                trigger="mention" if self.mention_comment_id else "label",
                lock=await self._lock_policy(),
                ref=ref,
                issue=self.issue,
                fallback_ref_on_missing=True,
                use_max=self.issue.has_max_label(),
                sandbox_env_id=sandbox_env_id,
                persist_ref=True,
                arm_watch=True,
                recover_draft=True,
                extra_metadata={
                    "author": self.issue.author.username,
                    "triggered_by": triggered_by,
                    "issue_id": self.issue.iid,
                    "labels": [label.lower() for label in self.issue.labels],
                },
            ),
            RunHooks(on_success=self._on_success, on_failure=self._on_failure),
        )
        return outcome.agent_result

    def _input_message(self) -> tuple[HumanMessage, str]:
        """The turn's message and who triggered it: the mention comment, or the prompt the bot label implies."""
        if self.mention_comment_id:
            comment = self.client.get_issue_comment(self.repo_id, self.issue.iid, self.mention_comment_id).notes[-1]
            return (
                HumanMessage(name=comment.author.username, id=comment.id, content=comment.body),
                comment.author.username,
            )
        prompt = ADDRESS_ISSUE_PROMPT if self.issue.has_auto_label() else PLAN_ISSUE_PROMPT
        return (
            HumanMessage(
                name=self.issue.author.username, id=str(self.issue.iid), content=prompt.format(issue_iid=self.issue.iid)
            ),
            self.issue.author.username,
        )

    async def _on_success(self, outcome: RunOutcome) -> None:
        if outcome.pending_question is not None:
            self._leave_comment(f"{outcome.response_text}\n\n{self._question_footer()}", reply_to_id=self.reply_to_id)
            return
        if response := outcome.response_text.strip():
            self._leave_comment(response)
        else:
            logger.warning("Agent returned empty response for issue %d", self.issue.iid)
            self._add_unable_to_address_issue_note()

    async def _on_failure(self, exc: Exception, *, draft_published: bool, snapshot: StateSnapshot | None) -> None:
        if isinstance(exc, AgentConfigurationError):
            logger.warning("issue_addressor: %s", exc)
            if self._claim_unable_note():
                self._leave_comment(
                    f"@{self.issue.author.username} I can't run yet: {exc}", reply_to_id=self.reply_to_id
                )
            return
        self._add_unable_to_address_issue_note(draft_published=draft_published)

    def _add_unable_to_address_issue_note(self, *, draft_published: bool = False):
        """
        Add a note to the issue to inform the user that the response could not be generated.
        """
        if not self._claim_unable_note():
            return
        self._leave_comment(
            render_to_string(
                "webhooks/unable_address_issue.txt",
                {
                    "bot_name": BOT_NAME,
                    "bot_username": self.client.current_user.username,
                    "draft_published": draft_published,
                    "is_gitlab": self.client.git_platform == GitPlatform.GITLAB,
                    "is_github": self.client.git_platform == GitPlatform.GITHUB,
                },
            ),
            reply_to_id=self.reply_to_id,
        )

    def _leave_comment(self, body: str, reply_to_id: str | None = None):
        """
        Leave a comment on the issue.

        Args:
            body: The body of the comment.
            reply_to_id: The ID of the comment to reply to. This is not supported for GitHub.
        """
        return self.client.create_issue_comment(self.repo_id, self.issue.iid, body, reply_to_id=reply_to_id)
