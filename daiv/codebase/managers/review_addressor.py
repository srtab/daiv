from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig
from unidiff import LINE_TYPE_CONTEXT, Hunk, PatchedFile
from unidiff.patch import Line, PatchSet

from automation.agents.review_addressor.agent import ReviewAddressorAgent
from automation.agents.review_addressor.conf import settings as review_addressor_settings
from automation.agents.review_addressor.schemas import ReviewContext
from codebase.base import Note, NoteDiffPosition, NoteDiffPositionType, NotePositionType, NoteType, SimpleDiscussion
from codebase.clients.base import Emoji
from codebase.utils import note_mentions_daiv, notes_to_messages, redact_diff_content

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


class ReviewAddressorManager(BaseManager):
    """
    Manages the code review process.
    """

    def __init__(self, *, merge_request_id: int, runtime_ctx: RuntimeCtx):
        super().__init__(runtime_ctx=runtime_ctx)
        self.merge_request_id = merge_request_id
        self.note_processor = NoteProcessor()

    @classmethod
    async def process_review_comments(cls, *, merge_request_id: int, runtime_ctx: RuntimeCtx):
        """
        Process review comments for merge request left in the diff that mention DAIV.

        Args:
            merge_request_id: The merge request ID.
            runtime_ctx: The runtime context.
        """
        manager = cls(merge_request_id=merge_request_id, runtime_ctx=runtime_ctx)

        if review_contexts := manager._get_review_context(manager._extract_merge_request_diffs()):
            await manager._address_review_context(review_contexts)

    @classmethod
    async def process_comments(cls, *, merge_request_id: int, runtime_ctx: RuntimeCtx):
        """
        Process comments left directly on the merge request (not in the diff or thread) that mention DAIV.

        Args:
            merge_request_id (int): The merge request ID.
            runtime_ctx (RuntimeCtx): The runtime context.
        """
        manager = cls(merge_request_id=merge_request_id, runtime_ctx=runtime_ctx)

        if context := manager._get_comments_context(manager._extract_merge_request_diffs()):
            await manager._address_review_context([context])

    async def _address_review_context(self, review_contexts: list[ReviewContext]):
        """
        Process code review discussion.

        If the discussion is resolved, it will save the file changes to be committed later.
        Each iteration of dicussions resolution will be processed with the changes from the previous iterations,
        ensuring that the file changes are processed correctly.

        Args:
            review_contexts: The list of review contexts to address.
        """
        config = RunnableConfig(
            tags=[review_addressor_settings.NAME, str(self.client.client_slug)],
            metadata={"merge_request_id": self.merge_request_id},
            recursion_limit=review_addressor_settings.RECURSION_LIMIT,
            configurable={
                "source_repo_id": self.ctx.repo_id,
                "source_ref": self.ctx.repo.active_branch.name,
                "bot_username": self.ctx.bot_username,
            },
        )

        reviewer_addressor = await ReviewAddressorAgent.get_runnable(
            store=self.store, skip_format_code=not self.ctx.config.sandbox.format_code_enabled
        )

        started_discussions: list[SimpleDiscussion] = []
        resolved_discussions: list[SimpleDiscussion] = []

        try:
            async for result in reviewer_addressor.astream(
                {"to_review": review_contexts}, config, stream_mode="custom", context=self.ctx
            ):
                discussion = result["review_context"].discussion

                if result.get("plan_and_execute") == "starting":
                    started_discussions.append(discussion)
                    self.client.create_merge_request_note_emoji(
                        self.ctx.repo_id, self.merge_request_id, Emoji.THUMBSUP, result["review_context"].notes[-1].id
                    )
                elif result.get("plan_and_execute") == "completed":
                    started_discussions.remove(discussion)
                    resolved_discussions.append(discussion)

                if result.get("reply_reviewer") == "starting":
                    self.client.create_merge_request_note_emoji(
                        self.ctx.repo_id, self.merge_request_id, Emoji.THUMBSUP, result["review_context"].notes[-1].id
                    )

                if result.get("reply"):
                    self.client.create_merge_request_comment(
                        self.ctx.repo_id,
                        self.merge_request_id,
                        result["reply"],
                        reply_to_id=discussion.id if discussion.is_thread else None,
                    )
        except Exception:
            logger.exception("Error processing review comments")
            for discussion in started_discussions:
                self.client.create_merge_request_comment(
                    self.ctx.repo_id,
                    self.merge_request_id,
                    (
                        "⚠️ I was unable to address the request due to an unexpected error. "
                        "Reply to this comment to try again."
                    ),
                    reply_to_id=discussion.id if discussion.is_thread else None,
                )
        finally:
            if self.git_manager.is_dirty():
                await self._commit_changes()

            for discussion in resolved_discussions:
                if discussion.is_resolvable and discussion.resolve_id:
                    self.client.mark_merge_request_comment_as_resolved(
                        self.ctx.repo_id, self.merge_request_id, discussion.resolve_id
                    )

    def _extract_merge_request_diffs(self) -> dict[str, PatchedFile]:
        """
        Extract patch files from merge request.
        """
        merge_request = self.client.get_merge_request(self.ctx.repo_id, self.merge_request_id)

        patch_set_all: PatchSet = redact_diff_content(
            # prefix the branches with "origin/" to get the diff from the remote repository.
            self.ctx.repo.git.diff(
                f"origin/{merge_request.target_branch}..origin/{merge_request.source_branch}", "--patch", "--binary"
            ),
            self.ctx.config.omit_content_patterns,
            as_patch_set=True,
        )

        merge_request_patches: dict[str, PatchedFile] = {patch_file.path: patch_file for patch_file in patch_set_all}
        merge_request_patches["__all__"] = patch_set_all
        return merge_request_patches

    def _get_review_context(self, merge_request_patches: dict[str, PatchedFile]) -> list[ReviewContext]:
        """
        Extract discussions data from merge request to be addressed by the agent.

        It will extract the discussions that are not resolved and that have DAIV mentions in the latest note.
        """
        review_contexts = []

        for discussion in self.client.get_merge_request_review_comments(self.ctx.repo_id, self.merge_request_id):
            if not discussion.notes:
                logger.info("Ignoring discussion, no notes: %s", discussion.id)
                continue

            if discussion.notes[-1].author.id == self.client.current_user.id:
                logger.info("Ignoring discussion, DAIV is the author: %s", discussion.id)
                continue

            if not (note_mentions_daiv(discussion.notes[-1].body, self.client.current_user)):
                logger.info("Ignoring discussion, no DAIV mention in latest note: %s", discussion.id)
                continue

            context = ReviewContext(discussion=discussion.as_simple())

            for note in discussion.notes:
                if note.type == NoteType.DISCUSSION_NOTE:
                    context.diff = merge_request_patches["__all__"].__str__()

                elif note.type == NoteType.DIFF_NOTE:
                    if not note.position:
                        logger.warning("Ignoring note, no position defined: %s", note.id)
                        continue

                    path = note.position.new_path or note.position.old_path

                    if path not in merge_request_patches:
                        logger.warning("Ignoring note, path not found in patches: %s", note.id)
                        continue

                    if context.diff is None:
                        # This logic assumes that all notes will have the same patch file
                        context.diff = self.note_processor.extract_diff(
                            note, merge_request_patches[path], self._get_file_content(path)
                        )
                else:
                    raise ValueError(f"Unsupported note type: {note.type}")

                context.notes.append(note)

            if context.notes:
                context.notes = notes_to_messages(context.notes, self.client.current_user.id)
                review_contexts.append(context)
        return review_contexts

    def _get_comments_context(self, merge_request_patches: dict[str, PatchedFile]) -> ReviewContext | None:
        """
        Get the comments context from the merge request.
        """
        comments = self.client.get_merge_request_comments(self.ctx.repo_id, self.merge_request_id)

        latest_comment = comments[-1]

        if not latest_comment.notes:
            logger.info("Ignoring merge request: %s, no notes found in latest comment.", self.merge_request_id)
            return None

        if not note_mentions_daiv(latest_comment.notes[-1].body, self.client.current_user):
            logger.info("Ignoring merge request: %s, no DAIV mentions in latest comment.", self.merge_request_id)
            return None

        review_context = ReviewContext(
            discussion=latest_comment.as_simple(), diff=merge_request_patches["__all__"].__str__()
        )

        # If the latest comment is a thread, we only include the notes from the thread.
        # This is only relevant for GitLab, as in GitHub the comments are not threaded.
        if latest_comment.is_thread:
            review_context.notes.extend(notes_to_messages(latest_comment.notes, self.client.current_user.id))
        else:
            # If the latest comment is not a thread, we include all the comments.
            for comment in comments:
                if not comment.notes:
                    logger.info("Ignoring comment with no notes: %s", comment.id)
                    continue

                review_context.notes.extend(notes_to_messages(comment.notes, self.client.current_user.id))

        return review_context

    def _get_file_content(self, path: str) -> str:
        """
        Get the file content from the repository.
        """
        resolved_file_path = (Path(self.ctx.repo.working_dir) / path).resolve()

        if (
            not resolved_file_path.exists()
            or not resolved_file_path.is_file()
            or any(fnmatch.fnmatch(path, pattern) for pattern in self.ctx.config.combined_exclude_patterns)
        ):
            raise FileNotFoundError(f"File content '{path}' not found in repository.")

        return resolved_file_path.read_text()
