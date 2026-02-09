from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings as django_settings
from django.template.loader import render_to_string

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from unidiff import LINE_TYPE_CONTEXT, Hunk, PatchedFile
from unidiff.patch import Line

from automation.agent.graph import create_daiv_agent
from automation.agent.publishers import GitChangePublisher
from automation.agent.utils import extract_text_content, get_daiv_agent_kwargs
from codebase.base import GitPlatform, MergeRequest, Note, NoteDiffPosition, NoteDiffPositionType, NotePositionType
from core.constants import BOT_NAME
from core.utils import generate_uuid

from .base import BaseManager

if TYPE_CHECKING:
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

    def __init__(self, *, merge_request: MergeRequest, mention_comment_id: str, runtime_ctx: RuntimeCtx):
        super().__init__(runtime_ctx=runtime_ctx)
        self.merge_request = merge_request
        self.mention_comment_id = mention_comment_id
        self.thread_id = generate_uuid(f"{self.ctx.repo_id}:{self.ctx.scope}/{self.merge_request.merge_request_id}")

    @classmethod
    async def address_comments(cls, *, merge_request: MergeRequest, mention_comment_id: str, runtime_ctx: RuntimeCtx):
        """
        Process comments left directly on the merge request (not in the diff or thread) that mention DAIV.

        Args:
            merge_request (MergeRequest): The merge request.
            mention_comment_id (str): The mention comment id.
            runtime_ctx (RuntimeCtx): The runtime context.
        """
        manager = cls(merge_request=merge_request, mention_comment_id=mention_comment_id, runtime_ctx=runtime_ctx)

        try:
            await manager._address_comments()
        except Exception as e:
            logger.exception("Error addressing comments for merge request %d: %s", merge_request.merge_request_id, e)
            manager._add_unable_to_address_review_note()

    async def _address_comments(self):
        """
        Process comments left directly on the merge request (not in the diff or thread) that mention DAIV.
        """
        mention_comment = self.client.get_merge_request_comment(
            self.ctx.repo_id, self.merge_request.merge_request_id, self.mention_comment_id
        )

        async with AsyncPostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
            daiv_agent = await create_daiv_agent(
                ctx=self.ctx,
                checkpointer=checkpointer,
                store=self.store,
                **get_daiv_agent_kwargs(model_config=self.ctx.config.models.agent),
            )
            agent_config = RunnableConfig(
                configurable={"thread_id": self.thread_id},
                tags=[daiv_agent.get_name(), self.client.git_platform.value],
                metadata={
                    "author": self.merge_request.author.username,
                    "merge_request_id": self.merge_request.merge_request_id,
                    "scope": self.ctx.scope,
                },
            )

            try:
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
                snapshot = await daiv_agent.aget_state(config=agent_config)

                # If and unexpect error occurs while addressing the issue, a draft merge request is created to avoid
                # losing the changes made by the agent.
                publisher = GitChangePublisher(self.ctx)
                publish_result = await publisher.publish(
                    branch_name=snapshot.values.get("branch_name"),
                    merge_request_id=snapshot.values.get("merge_request_id"),
                    skip_ci=True,
                    as_draft=True,
                )

                # If the draft merge request is created successfully, we update the state to reflect the new MR.
                if publish_result:
                    await daiv_agent.aupdate_state(
                        config=agent_config,
                        values={
                            "branch_name": publish_result["branch_name"],
                            "merge_request_id": publish_result["merge_request_id"],
                        },
                    )

                self._add_unable_to_address_review_note(changes_published=bool(publish_result))
            else:
                self._leave_comment(result and extract_text_content(result["messages"][-1].content))

    def _add_unable_to_address_review_note(self, *, changes_published: bool = False):
        """
        Add a note to the merge request to inform the user that the review could not be addressed.

        Args:
            changes_published: Whether the changes were published to the repository.
        """
        self._leave_comment(
            render_to_string(
                "codebase/review_unable_address_review.txt",
                {
                    "bot_name": BOT_NAME,
                    "bot_username": self.ctx.bot_username,
                    "changes_published": changes_published,
                    "is_gitlab": self.ctx.git_platform == GitPlatform.GITLAB,
                },
            ),
            reply_to_id=self.mention_comment_id,
        )

    def _leave_comment(self, body: str, reply_to_id: str | None = None):
        """
        Create a comment on the merge request.

        Args:
            body: The body of the comment.
            reply_to_id: The ID of the comment to reply to.
        """
        return self.client.create_merge_request_comment(
            self.ctx.repo_id, self.merge_request.merge_request_id, body, reply_to_id=reply_to_id
        )
