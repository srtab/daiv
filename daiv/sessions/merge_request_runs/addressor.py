from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from langchain_core.messages import HumanMessage

from automation.agent.provider_errors import provider_failure_of
from automation.agent.references import refs_from_stored
from automation.agent.utils import repo_relative_text
from codebase.base import GitPlatform, MergeRequest, Scope
from codebase.clients.base import CommentThread
from codebase.exceptions import CloneRefNotFoundError
from core.constants import BOT_NAME
from sessions.executor.run import execute_run
from sessions.executor.spec import RunHooks, RunSpec
from sessions.merge_request_runs.base import BaseManager

if TYPE_CHECKING:
    from langgraph.types import StateSnapshot

    from automation.agent.references import ExternalRef
    from sessions.executor.result import AgentResult
    from sessions.executor.spec import RunOutcome

logger = logging.getLogger("daiv.agents")


class CommentsAddressorManager(BaseManager):
    """
    Runs the agent on a merge request comment that mentions DAIV and answers on the merge request.

    Deliberately does **not** arm the CI watch (``arm_watch=False``): this pushes to a merge request someone
    else may own. Pinned by ``test_a_successful_run_neither_moves_the_ref_nor_arms_the_watch``.
    """

    def __init__(self, *, merge_request: MergeRequest, **kwargs):
        super().__init__(**kwargs)
        self.merge_request = merge_request

    @classmethod
    async def address_comments(
        cls,
        *,
        repo_id: str,
        merge_request: MergeRequest,
        mention_comment_id: str | None,
        thread_id: str,
        use_max: bool,
        mention_note_id: int | None = None,
        sandbox_env_id: str | None = None,
        run_id: str | None = None,
        acting_user_id: int | None = None,
        prompt: str | None = None,
        requester_username: str = "",
        agent_model: str | None = None,
        agent_thinking_level: str | None = None,
        references: list[dict] | None = None,
        mcp_overrides: dict[str, str] | None = None,
    ) -> AgentResult:
        """
        Process a comment left directly on the merge request (not in the diff or thread) that mentions DAIV, or a
        job's ``prompt``, whose answer and question go on the merge request as new comments.

        Args:
            repo_id: The repository slug.
            merge_request: The merge request.
            mention_comment_id: The mention comment id.
            thread_id: The session's thread id.
            use_max: The max mode the callback decided.
            mention_note_id: The note that mentioned the bot, which the run answers.
            sandbox_env_id: The sandbox environment the callback selected.
            run_id: The ``Run`` row this turn executes, or ``None`` when the callback created none.
            acting_user_id: The DAIV user the run acts as, its ``Run``'s user; ``None`` for none.
            prompt: A job's prompt, in place of the mention's note.
            requester_username: Who sent the job.
            agent_model: The job's model, ``None`` for the default.
            agent_thinking_level: The thinking level for ``agent_model``.
            references: A job's stored external references.
            mcp_overrides: A job's resolved MCP overrides.

        Returns:
            An :class:`AgentResult` dict with the agent response and code_changes flag. A vanished source branch
            raises ``CloneRefNotFoundError`` for the task to answer.
        """
        manager = cls(
            repo_id=repo_id,
            merge_request=merge_request,
            mention_comment_id=mention_comment_id,
            mention_note_id=mention_note_id,
            thread_id=thread_id,
        )

        try:
            if prompt is not None:
                request = HumanMessage(name=requester_username or None, content=prompt)
                return await manager._run(
                    request,
                    asker=requester_username,
                    trigger="job",
                    sandbox_env_id=sandbox_env_id,
                    run_id=run_id,
                    use_max=use_max,
                    acting_user_id=acting_user_id,
                    agent_model=agent_model,
                    agent_thinking_level=agent_thinking_level,
                    references=refs_from_stored(references or []),
                    mcp_overrides=mcp_overrides or {},
                )
            return await manager._address_comments(
                sandbox_env_id=sandbox_env_id, run_id=run_id, use_max=use_max, acting_user_id=acting_user_id
            )
        except CloneRefNotFoundError:
            raise
        except Exception:
            manager._add_unable_to_address_review_note()
            raise

    async def _address_comments(
        self, *, sandbox_env_id: str | None, run_id: str | None, use_max: bool, acting_user_id: int | None = None
    ) -> AgentResult:
        note = self.client.get_merge_request_note(
            self.repo_id, self.merge_request.merge_request_id, self._require_mention_note_id()
        )
        return await self._run(
            HumanMessage(name=note.author.username, id=note.id, content=note.body),
            asker=note.author.username,
            trigger="mention",
            sandbox_env_id=sandbox_env_id,
            run_id=run_id,
            use_max=use_max,
            acting_user_id=acting_user_id,
        )

    async def _run(
        self,
        request: HumanMessage,
        *,
        asker: str,
        trigger: str,
        sandbox_env_id: str | None,
        run_id: str | None,
        use_max: bool,
        acting_user_id: int | None,
        agent_model: str | None = None,
        agent_thinking_level: str | None = None,
        references: tuple[ExternalRef, ...] = (),
        mcp_overrides: dict[str, str] | None = None,
    ) -> AgentResult:
        outcome = await execute_run(
            RunSpec(
                thread_id=self.thread_id,
                repo_id=self.repo_id,
                scope=Scope.MERGE_REQUEST,
                input_messages=(request,),
                trigger=trigger,
                lock=self._lock_policy(),
                ref=self.merge_request.source_branch,
                merge_request=self.merge_request,
                use_max=use_max,
                agent_model=agent_model,
                agent_thinking_level=agent_thinking_level,
                sandbox_env_id=sandbox_env_id,
                acting_user_id=acting_user_id,
                mcp_overrides=mcp_overrides or {},
                references=references,
                run_id=run_id,
                recover_draft=True,
                extra_metadata={
                    "author": self.merge_request.author.username,
                    "triggered_by": asker,
                    "merge_request_id": self.merge_request.merge_request_id,
                },
            ),
            RunHooks(on_success=partial(self._on_success, asker=asker), on_failure=self._on_failure),
        )
        return outcome.agent_result

    async def _on_success(self, outcome: RunOutcome, *, asker: str) -> None:
        fallback_footer = self._render_protected_branch_footer(outcome.snapshot)
        if (question := self._question_comment(outcome, asker=asker)) is not None:
            self._leave_comment(self._append_footer(question, fallback_footer), reply_to_id=self.reply_to_id)
            return
        if response := outcome.response_text.strip():
            self._leave_comment(self._append_footer(response, fallback_footer))
        else:
            logger.warning("Agent returned empty response for merge request %d", self.merge_request.merge_request_id)
            self._add_unable_to_address_review_note(fallback_footer=fallback_footer)

    async def _on_failure(self, exc: Exception, *, draft_published: bool, snapshot: StateSnapshot | None) -> None:
        if isinstance(exc, CloneRefNotFoundError):
            return
        if self._said_cannot_run_yet(exc, author=self.merge_request.author.username):
            return
        failure = provider_failure_of(exc)
        self._add_unable_to_address_review_note(
            draft_published=draft_published,
            fallback_footer=self._render_protected_branch_footer(snapshot),
            failure_message=failure.message() if failure else None,
        )

    def _render_protected_branch_footer(self, snapshot) -> str | None:
        """
        Render the protected-branch fallback footer when the publisher swapped to a
        fresh MR during this run, so the notice can be bundled into the agent's
        reply instead of posted as a separate comment on the original MR.
        """
        if snapshot is None:
            return None

        source_branch = snapshot.values.get("protected_branch_fallback_source")
        new_mr = snapshot.values.get("merge_request")
        if not source_branch:
            # No protected-branch fallback happened this run (the common case), so there is
            # no footer to render. ``merge_request`` being set here is normal MR-scope state,
            # not a partial checkpoint, so stay silent.
            return None
        if new_mr is None:
            # The publisher writes ``protected_branch_fallback_source`` and ``merge_request``
            # together when it swaps to a fresh MR; a fallback source with no MR means the
            # checkpoint was raced/partial. The user gets the reply with no breadcrumb to the
            # new MR in that case — surface it to the operator.
            logger.warning(
                "Partial protected-branch fallback state on MR %d "
                "(source_branch=%r, merge_request=%r); dropping footer.",
                self.merge_request.merge_request_id,
                source_branch,
                new_mr,
            )
            return None

        return render_to_string(
            "automation/protected_branch_fallback.txt",
            {
                "source_branch": source_branch,
                "new_merge_request_url": new_mr.web_url,
                "new_merge_request_id": new_mr.merge_request_id,
                "is_gitlab": self.client.git_platform == GitPlatform.GITLAB,
            },
        )

    def _add_unable_to_address_review_note(
        self, *, draft_published: bool = False, fallback_footer: str | None = None, failure_message: str | None = None
    ):
        """
        Add a note to the merge request to inform the user that the review could not be addressed.

        Args:
            draft_published: Whether the draft merge request was published to the repository.
            fallback_footer: Pre-rendered protected-branch fallback footer to bundle into
                the note when the publisher swapped to a fresh MR.
            failure_message: Fixed provider-failure text that replaces the generic first line.
        """
        if not self._claim_unable_note():
            return
        body = render_to_string(
            "core/unable_address_review.txt",
            {
                "bot_name": BOT_NAME,
                "bot_username": self.client.current_user.username,
                "draft_published": draft_published,
                "failure_message": failure_message,
                "is_gitlab": self.client.git_platform == GitPlatform.GITLAB,
            },
        )
        self._leave_comment(self._append_footer(body, fallback_footer), reply_to_id=self.reply_to_id)

    def _leave_comment(self, body: str, reply_to_id: str | None = None):
        """
        Create a comment on the merge request.

        Args:
            body: The body of the comment.
            reply_to_id: The ID of the comment to reply to.
        """
        return self.client.create_merge_request_comment(
            self.repo_id,
            self.merge_request.merge_request_id,
            repo_relative_text(body),
            reply_to=CommentThread(reply_to_id) if reply_to_id else None,
        )
