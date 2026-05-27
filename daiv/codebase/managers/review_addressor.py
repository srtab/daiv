from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from langchain_core.messages import HumanMessage
from redis.exceptions import RedisError
from unidiff import LINE_TYPE_CONTEXT, Hunk, PatchedFile
from unidiff.patch import Line

from automation.agent.graph import create_daiv_agent
from automation.agent.usage_tracking import build_usage_summary, track_usage_metadata
from automation.agent.utils import build_langsmith_config, extract_text_content, get_daiv_agent_kwargs
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
from codebase.utils import compute_thread_id
from core.checkpointer import open_checkpointer
from core.constants import BOT_NAME

from .base import BaseManager

if TYPE_CHECKING:
    from automation.agent.results import AgentResult
    from codebase.context import RuntimeCtx

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
    Manages the comments addressing process.
    """

    def __init__(
        self,
        *,
        merge_request: MergeRequest,
        mention_comment_id: str,
        runtime_ctx: RuntimeCtx,
        thread_id: str | None = None,
    ):
        super().__init__(runtime_ctx=runtime_ctx)
        self.merge_request = merge_request
        self.mention_comment_id = mention_comment_id
        if thread_id is None:
            thread_id = compute_thread_id(
                repo_slug=self.ctx.repository.slug,
                scope=Scope.MERGE_REQUEST,
                entity_iid=self.merge_request.merge_request_id,
            )
        elif not thread_id:
            raise ValueError(f"thread_id must be non-empty or None, got {thread_id!r}")
        self.thread_id = thread_id

    @classmethod
    async def address_comments(
        cls,
        *,
        merge_request: MergeRequest,
        mention_comment_id: str,
        runtime_ctx: RuntimeCtx,
        thread_id: str | None = None,
    ) -> AgentResult:
        """
        Process comments left directly on the merge request (not in the diff or thread) that mention DAIV.

        Args:
            merge_request (MergeRequest): The merge request.
            mention_comment_id (str): The mention comment id.
            runtime_ctx (RuntimeCtx): The runtime context.

        Returns:
            An :class:`AgentResult` dict with the agent response and code_changes flag.
        """
        manager = cls(
            merge_request=merge_request,
            mention_comment_id=mention_comment_id,
            runtime_ctx=runtime_ctx,
            thread_id=thread_id,
        )

        try:
            return await manager._address_comments()
        except AgentConfigurationError as err:
            logger.warning("review_addressor: %s", err)
            manager._leave_comment(
                f"@{manager.merge_request.author.username} I can't run yet: {err}",
                reply_to_id=manager.mention_comment_id if manager.ctx.git_platform == GitPlatform.GITLAB else None,
            )
            raise
        except Exception:
            manager._add_unable_to_address_review_note()
            raise

    async def _address_comments(self) -> AgentResult:
        """
        Process comments left directly on the merge request (not in the diff or thread) that mention DAIV.
        """
        mention_comment = self.client.get_merge_request_comment(
            self.ctx.repository.slug, self.merge_request.merge_request_id, self.mention_comment_id
        )

        async with open_checkpointer() as checkpointer:
            agent_kwargs = get_daiv_agent_kwargs(model_config=self.ctx.config.models.agent)
            daiv_agent = await create_daiv_agent(
                ctx=self.ctx, checkpointer=checkpointer, store=self.store, **agent_kwargs
            )
            agent_config = build_langsmith_config(
                self.ctx,
                trigger="mention",
                model=agent_kwargs["model_names"][0],
                thinking_level=agent_kwargs["thinking_level"],
                agent_name=daiv_agent.get_name(),
                configurable={"thread_id": self.thread_id},
                extra_metadata={
                    "author": self.merge_request.author.username,
                    "triggered_by": mention_comment.notes[0].author.username,
                    "merge_request_id": self.merge_request.merge_request_id,
                },
            )
            try:
                with track_usage_metadata() as usage_handler:
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
                        config=agent_config,
                        context=self.ctx,
                    )
            except Exception:
                draft_published = await self._recover_draft(
                    daiv_agent,
                    agent_config,
                    entity_label="merge request",
                    entity_id=self.merge_request.merge_request_id,
                )
                # _recover_draft may have updated state with the recovered MR, so
                # re-read here rather than reusing a pre-recovery snapshot.
                fallback_footer = self._render_protected_branch_footer(
                    await self._safe_get_state(daiv_agent, agent_config)
                )
                self._add_unable_to_address_review_note(
                    draft_published=draft_published, fallback_footer=fallback_footer
                )
                raise
            else:
                response_text = ""
                # Read the snapshot once so the fallback footer and AgentResult share
                # the same checkpoint read instead of round-tripping Redis twice.
                snapshot = await self._safe_get_state(daiv_agent, agent_config)
                fallback_footer = self._render_protected_branch_footer(snapshot)
                if (
                    result
                    and "messages" in result
                    and result["messages"]
                    and (response_text := extract_text_content(result["messages"][-1].content).strip())
                ):
                    self._leave_comment(self._append_footer(response_text, fallback_footer))
                else:
                    logger.warning(
                        "Agent returned empty response for merge request %d (result keys: %s)",
                        self.merge_request.merge_request_id,
                        list(result.keys()) if result else None,
                    )
                    self._add_unable_to_address_review_note(fallback_footer=fallback_footer)

                return await self._build_agent_result(
                    daiv_agent,
                    agent_config,
                    response=response_text,
                    usage=build_usage_summary(usage_handler).to_dict(),
                    snapshot=snapshot,
                )

    async def _safe_get_state(self, agent, config):
        """Read agent state, returning None on transport/serialization failure."""
        try:
            return await agent.aget_state(config=config)
        except RedisError, OSError, json.JSONDecodeError:
            logger.warning(
                "Failed to read agent state for merge request %d", self.merge_request.merge_request_id, exc_info=True
            )
            return None

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
        if not source_branch and new_mr is None:
            return None
        if not source_branch or new_mr is None:
            # The publisher writes the two fields together; seeing only one set means
            # the checkpoint was raced/partial. The user gets the reply with no
            # breadcrumb to the new MR in that case — surface it to the operator.
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
                "is_gitlab": self.ctx.git_platform == GitPlatform.GITLAB,
            },
        )

    @staticmethod
    def _append_footer(body: str, footer: str | None) -> str:
        if not footer:
            return body
        return f"{body.rstrip()}\n\n{footer.lstrip()}"

    def _add_unable_to_address_review_note(self, *, draft_published: bool = False, fallback_footer: str | None = None):
        """
        Add a note to the merge request to inform the user that the review could not be addressed.

        Args:
            draft_published: Whether the draft merge request was published to the repository.
            fallback_footer: Pre-rendered protected-branch fallback footer to bundle into
                the note when the publisher swapped to a fresh MR.
        """
        body = render_to_string(
            "codebase/unable_address_review.txt",
            {
                "bot_name": BOT_NAME,
                "bot_username": self.ctx.bot_username,
                "draft_published": draft_published,
                "is_gitlab": self.ctx.git_platform == GitPlatform.GITLAB,
            },
        )
        self._leave_comment(
            self._append_footer(body, fallback_footer),
            # GitHub doesn't support replying to comments, so we need to provide a reply_to_id only for GitLab.
            reply_to_id=self.mention_comment_id if self.ctx.git_platform == GitPlatform.GITLAB else None,
        )

    def _leave_comment(self, body: str, reply_to_id: str | None = None):
        """
        Create a comment on the merge request.

        Args:
            body: The body of the comment.
            reply_to_id: The ID of the comment to reply to.
        """
        return self.client.create_merge_request_comment(
            self.ctx.repository.slug, self.merge_request.merge_request_id, body, reply_to_id=reply_to_id
        )
