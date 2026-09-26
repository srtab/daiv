from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from langchain_core.messages import HumanMessage
from sessions.executor.run import execute_run
from sessions.executor.spec import RunHooks, RunSpec
from unidiff import LINE_TYPE_CONTEXT, Hunk, PatchedFile
from unidiff.patch import Line

from automation.agent.validators import AgentConfigurationError
from codebase.base import (
    GitPlatform,
    MergeRequest,
    Note,
    NoteDiffPosition,
    NoteDiffPositionType,
    NotePositionType,
    Scope,
)
from codebase.exceptions import CloneRefNotFoundError
from codebase.utils import resolve_thread_id
from core.constants import BOT_NAME

from .base import BaseManager

if TYPE_CHECKING:
    from langgraph.types import StateSnapshot
    from sessions.executor.spec import RunOutcome

    from automation.agent.results import AgentResult

logger = logging.getLogger("daiv.agents")


class NoteProcessor:
    """
    Processes text-based diff notes.
    """

    def extract_diff(self, note: Note, patch_file: PatchedFile, file_content: str) -> str | None:
        """
        Extract diff content where the note was left.

        Args:
            note: The note containing position information
            patch_file: The patch file to extract content from
            file_content: The file content to extract content from when the note is an expanded line range

        Returns:
            str | None: The extracted diff content or None if extraction fails
        """
        if not note.position:
            return None

        if note.position.position_type == NotePositionType.FILE:
            return str(patch_file)
        elif note.position.position_type == NotePositionType.TEXT:
            if note.position.line_range:
                # Extract line range information
                start_info = self._get_line_info(note.position.line_range.start)
                end_info = self._get_line_info(note.position.line_range.end)
            else:
                # There are cases of single line notes, where the line range is not defined.
                # In this case, we will use the old or new line information to build the diff content.
                _position = self._get_line_info(
                    NoteDiffPosition(
                        type=NoteDiffPositionType.OLD if note.position.old_line else NoteDiffPositionType.NEW,
                        old_line=note.position.old_line,
                        new_line=note.position.new_line,
                    )
                )
                start_info = _position
                end_info = _position

            return self._build_diff_content(
                self._merge_hunks_from_patch_file(patch_file, file_content), start_info, end_info
            )
        return None

    def _merge_hunks_from_patch_file(self, patch_file: PatchedFile, original_content: str) -> PatchedFile:
        """
        Merge all hunks from patch file into a single hunk with the whole file content.

        This simplifies the diff processing as we can treat the patch file as a single hunk. Specially for the case
        where the note is an expanded line range.

        Args:
            patch_file: The patch file to merge
            original_content: The original content of the file

        Returns:
            The patch file with the merged hunks
        """
        splitted_content = original_content.splitlines()

        unified_hunk = Hunk(src_start=1, src_len=len(splitted_content), tgt_start=1, tgt_len=len(splitted_content))

        # Add lines of original content to the unified hunk
        for line_no, line in enumerate(splitted_content, 1):
            unified_hunk.append(
                Line(
                    line + "\n", LINE_TYPE_CONTEXT, source_line_no=line_no, target_line_no=line_no, diff_line_no=line_no
                )
            )

        # Add hunks from patch file to the unified hunk
        hunk_offset = 0
        for hunk in patch_file:
            unified_hunk[
                hunk.target_start + hunk_offset - 1 : hunk.target_start + hunk.target_length + hunk_offset - 1
            ] = hunk
            # Extra lines from source added to the unified hunk, tipically removed lines
            hunk_offset += len([1 for line in hunk if line.target_line_no is None])

        # Normalize line numbers to be sequential
        source_line_no = 0
        target_line_no = 0
        for line in unified_hunk:
            if line.source_line_no is not None:
                source_line_no += 1
                line.source_line_no = source_line_no
            if line.target_line_no is not None:
                target_line_no += 1
                line.target_line_no = target_line_no

        # Adjust the source length to account for added and removed lines
        unified_hunk.source_length -= unified_hunk.added - unified_hunk.removed

        # Create a new patch file with the unified hunk
        new_patch_file = PatchedFile(source=patch_file.source_file, target=patch_file.target_file)
        new_patch_file.append(unified_hunk)
        return new_patch_file

    def _get_line_info(self, position: NoteDiffPosition) -> dict:
        """
        Extract line information from position.

        Args:
            position: The position containing line information

        Returns:
            dict: The line information
        """
        side = "target" if position.type != NoteDiffPositionType.OLD else "source"
        line_no = position.new_line if side == "target" else position.old_line
        return {"side": side, "line_no": line_no, "new_line": position.new_line, "old_line": position.old_line}

    def _build_diff_content(self, patch_file: PatchedFile, start_info: dict, end_info: dict) -> str | None:
        """
        Build diff content from patch file based on note position.

        Args:
            patch_file: The patch file to extract content from
            start_info: The start line information
            end_info: The end line information

        Returns:
            str | None: The extracted diff content or None if extraction fails
        """
        for patch_hunk in patch_file:
            diff_code_lines: list[Line] = []

            for patch_line in patch_hunk:
                start_side_line_no = getattr(patch_line, f"{start_info['side']}_line_no")
                end_side_line_no = getattr(patch_line, f"{end_info['side']}_line_no")

                if (start_side_line_no and start_side_line_no >= start_info["line_no"]) or (
                    # we need to check diff_code_lines here to only check the end_line_no after we have
                    # found the start_line_no.
                    # Otherwise, we might end up with a line that is not part of the diff code lines.
                    diff_code_lines
                    and (end_side_line_no is None or end_side_line_no and end_side_line_no <= end_info["line_no"])
                ):
                    diff_code_lines.append(patch_line)

                if end_side_line_no and end_info["line_no"] == end_side_line_no:
                    break

            hunk = Hunk(
                src_start=diff_code_lines[0].source_line_no or diff_code_lines[0].target_line_no,
                src_len=len([line for line in diff_code_lines if line.is_context or line.is_removed]),
                tgt_start=diff_code_lines[0].target_line_no or diff_code_lines[0].source_line_no,
                tgt_len=len([line for line in diff_code_lines if line.is_context or line.is_added]),
            )
            hunk.extend(diff_code_lines)

            # Extract the first two lines of the patch file to get the header
            diff_header = "\n".join(str(patch_file).splitlines()[:2]) + "\n"
            return diff_header + str(hunk)
        return None


class CommentsAddressorManager(BaseManager):
    """
    Runs the agent on a merge request comment that mentions DAIV and answers on the merge request.

    Deliberately does **not** arm the CI watch (``arm_watch=False``): this pushes to a merge request someone
    else may own. Pinned by ``test_a_successful_run_neither_moves_the_ref_nor_arms_the_watch``.
    """

    def __init__(
        self, *, repo_id: str, merge_request: MergeRequest, mention_comment_id: str, thread_id: str | None = None
    ):
        super().__init__(
            repo_id=repo_id,
            thread_id=resolve_thread_id(
                thread_id, repo_slug=repo_id, scope=Scope.MERGE_REQUEST, entity_iid=merge_request.merge_request_id
            ),
            mention_comment_id=mention_comment_id,
        )
        self.merge_request = merge_request

    @classmethod
    async def address_comments(
        cls,
        *,
        repo_id: str,
        merge_request: MergeRequest,
        mention_comment_id: str,
        thread_id: str | None = None,
        sandbox_env_id: str | None = None,
    ) -> AgentResult:
        """
        Process comments left directly on the merge request (not in the diff or thread) that mention DAIV.

        Args:
            repo_id: The repository slug.
            merge_request: The merge request.
            mention_comment_id: The mention comment id.
            thread_id: The session's thread id; ``None`` computes the deterministic one.
            sandbox_env_id: The sandbox environment the callback selected.

        Returns:
            An :class:`AgentResult` dict with the agent response and code_changes flag. A vanished source branch
            raises ``CloneRefNotFoundError`` for the task to answer.
        """
        manager = cls(
            repo_id=repo_id, merge_request=merge_request, mention_comment_id=mention_comment_id, thread_id=thread_id
        )

        try:
            return await manager._address_comments(sandbox_env_id=sandbox_env_id)
        except CloneRefNotFoundError:
            raise
        except Exception:
            manager._add_unable_to_address_review_note()
            raise

    async def _address_comments(self, *, sandbox_env_id: str | None) -> AgentResult:
        note = self.client.get_merge_request_comment(
            self.repo_id, self.merge_request.merge_request_id, self.mention_comment_id
        ).notes[0]
        outcome = await execute_run(
            RunSpec(
                thread_id=self.thread_id,
                repo_id=self.repo_id,
                scope=Scope.MERGE_REQUEST,
                input_messages=(HumanMessage(name=note.author.username, id=note.id, content=note.body),),
                trigger="mention",
                lock=await self._lock_policy(),
                ref=self.merge_request.source_branch,
                merge_request=self.merge_request,
                sandbox_env_id=sandbox_env_id,
                recover_draft=True,
                extra_metadata={
                    "author": self.merge_request.author.username,
                    "triggered_by": note.author.username,
                    "merge_request_id": self.merge_request.merge_request_id,
                },
            ),
            RunHooks(on_success=self._on_success, on_failure=self._on_failure),
        )
        return outcome.agent_result

    async def _on_success(self, outcome: RunOutcome) -> None:
        fallback_footer = self._render_protected_branch_footer(outcome.snapshot)
        if (question := self._question_comment(outcome)) is not None:
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
        if isinstance(exc, AgentConfigurationError):
            logger.warning("review_addressor: %s", exc)
            if self._claim_unable_note():
                self._leave_comment(
                    f"@{self.merge_request.author.username} I can't run yet: {exc}", reply_to_id=self.reply_to_id
                )
            return
        self._add_unable_to_address_review_note(
            draft_published=draft_published, fallback_footer=self._render_protected_branch_footer(snapshot)
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

    def _add_unable_to_address_review_note(self, *, draft_published: bool = False, fallback_footer: str | None = None):
        """
        Add a note to the merge request to inform the user that the review could not be addressed.

        Args:
            draft_published: Whether the draft merge request was published to the repository.
            fallback_footer: Pre-rendered protected-branch fallback footer to bundle into
                the note when the publisher swapped to a fresh MR.
        """
        if not self._claim_unable_note():
            return
        body = render_to_string(
            "webhooks/unable_address_review.txt",
            {
                "bot_name": BOT_NAME,
                "bot_username": self.client.current_user.username,
                "draft_published": draft_published,
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
            self.repo_id, self.merge_request.merge_request_id, body, reply_to_id=reply_to_id
        )
