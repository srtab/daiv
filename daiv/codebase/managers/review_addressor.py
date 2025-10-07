from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field

from django.conf import settings

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.memory import InMemoryStore
from unidiff import LINE_TYPE_CONTEXT, Hunk, PatchedFile
from unidiff.patch import Line

from automation.agents.nodes import apply_format_code_node
from automation.agents.review_addressor.agent import ReviewAddressorAgent
from automation.agents.review_addressor.conf import settings as review_addressor_settings
from automation.utils import get_file_changes
from codebase.base import Discussion, Note, NoteDiffPosition, NoteDiffPositionType, NotePositionType
from codebase.clients import RepoClient
from codebase.clients.base import Emoji
from codebase.context import get_repository_ctx
from codebase.utils import discussion_has_daiv_mentions, note_mentions_daiv, notes_to_messages
from core.utils import generate_uuid

from .base import BaseManager

logger = logging.getLogger("daiv.agents")


@dataclass
class DiscussionReviewContext:
    discussion: Discussion | None = None
    patch_file: PatchedFile | None = None
    notes: list[Note] = field(default_factory=list)
    diff: str | None = None


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
            hunk_offset = len([1 for line in hunk if line.target_line_no is None])

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

    def __init__(self, client: RepoClient, repo_id: str, ref: str | None = None, **kwargs):
        super().__init__(client, repo_id, ref)
        self.merge_request_id = kwargs["merge_request_id"]
        self.note_processor = NoteProcessor()

    @classmethod
    async def process_review_comments(cls, repo_id: str, merge_request_id: int, ref: str | None = None):
        """
        Process code review for merge request.
        """
        client = RepoClient.create_instance()
        manager = cls(client, repo_id, ref, merge_request_id=merge_request_id)

        resolved_discussions: list[tuple[str, str]] = []

        for context in manager._get_review_context(manager._extract_merge_request_diffs()):
            try:
                if result := await manager._address_review_context(context):
                    resolved_discussions.append(result)
            except Exception:
                # If there is an error, we will not resolve the discussion but we will continue to process the next one,
                # avoiding loosing the work done so far.
                logger.exception("Error processing discussion: %s", context.discussion.id)

        if await get_file_changes(manager._file_changes_store):
            await apply_format_code_node(manager._file_changes_store)
            await manager._commit_changes(file_changes=await get_file_changes(manager._file_changes_store))

        for discussion_id in resolved_discussions:
            manager.client.mark_merge_request_review_as_resolved(
                manager.repo_id, manager.merge_request_id, discussion_id
            )

    @classmethod
    async def process_comments(cls, repo_id: str, merge_request_id: int, ref: str | None = None):
        """
        Process comments left directly on the merge request (not in the diff) that mention DAIV.

        All comments are included in the same discussion as notes, so the agent has access to the conversation history.
        """
        client = RepoClient.create_instance()
        manager = cls(client, repo_id, ref, merge_request_id=merge_request_id)

        if context := manager._get_comments_context(manager._extract_merge_request_diffs()):
            try:
                await manager._address_review_context(context)
            except Exception:
                # If there is an error, we will not resolve the discussion but we will continue to process the next one,
                # avoiding loosing the work done so far.
                logger.exception("Error processing discussion: %s", context.discussion.id)

        if await get_file_changes(manager._file_changes_store):
            await apply_format_code_node(manager._file_changes_store)
            await manager._commit_changes(file_changes=await get_file_changes(manager._file_changes_store))

    async def _address_review_context(self, context: DiscussionReviewContext) -> tuple[str, str] | None:
        """
        Process code review discussion.

        If the discussion is resolved, it will save the file changes to be committed later.
        Each iteration of dicussions resolution will be processed with the changes from the previous iterations,
        ensuring that the file changes are processed correctly.
        """
        thread_id = generate_uuid(f"{self.repo_id}{self.ref}{self.merge_request_id}{context.discussion.id}")

        config = RunnableConfig(
            tags=[review_addressor_settings.NAME, str(self.client.client_slug)],
            metadata={
                "merge_request_id": self.merge_request_id,
                "discussion_id": context.discussion.id,
                "author": context.notes[-1].author.username,
            },
            configurable={
                "thread_id": thread_id,
                "source_repo_id": self.repo_id,
                "source_ref": self.ref,
                "bot_username": self.client.current_user.username,
            },
        )

        # Create a new store for each discussion.
        file_changes_store = InMemoryStore()
        # Pre-populate the store with file changes that resulted from previous discussions resolution.
        await self._set_file_changes(await get_file_changes(file_changes_store), store=file_changes_store)

        async with AsyncPostgresSaver.from_conn_string(settings.DB_URI) as checkpointer:
            reviewer_addressor = await ReviewAddressorAgent.get_runnable(
                store=file_changes_store, checkpointer=checkpointer
            )

            current_state = await reviewer_addressor.aget_state(config, subgraphs=True)
            if current_state.created_at is not None:
                # If the thread already exists, we will delete it to avoid conflicts. This is a workaround to avoid
                # conflicts when the same discussion is processed multiple times.
                await checkpointer.adelete_thread(thread_id)

            self.client.create_merge_request_note_emoji(
                self.repo_id, self.merge_request_id, Emoji.THUMBSUP, context.discussion.notes[-1].id
            )

            try:
                await reviewer_addressor.ainvoke(
                    {"notes": notes_to_messages(context.notes, self.client.current_user.id), "diff": context.diff},
                    config,
                )
            except Exception:
                logger.exception("Error processing discussion: %s", context.discussion.id)
                note_message = "⚠️ I encountered an unexpected error addressing the latest comment."
                if context.discussion.is_thread:
                    self.client.create_merge_request_review(
                        self.repo_id, self.merge_request_id, note_message, discussion_id=context.discussion.id
                    )
                else:
                    self.client.comment_merge_request(self.repo_id, self.merge_request_id, note_message)
                return None

            current_state = await reviewer_addressor.aget_state(config, subgraphs=True)

            if note := (current_state.values.get("reply") or current_state.values.get("plan_questions")):
                if context.discussion.is_thread:
                    self.client.create_merge_request_review(
                        self.repo_id, self.merge_request_id, note, discussion_id=context.discussion.id
                    )
                else:
                    self.client.comment_merge_request(self.repo_id, self.merge_request_id, note)

            elif files_to_commit := await get_file_changes(store=file_changes_store):
                # Update the global file changes store with file changes that resulted from the discussion resolution.
                await self._set_file_changes(files_to_commit)
                return context.discussion.id

        return None

    def _extract_merge_request_diffs(self) -> dict[str, PatchedFile]:
        """
        Extract patch files from merge request.
        """
        patch_set_all = self.client.get_merge_request_diff(self.repo_id, self.merge_request_id)
        merge_request_patches: dict[str, PatchedFile] = {patch_file.path: patch_file for patch_file in patch_set_all}
        merge_request_patches["__all__"] = patch_set_all
        return merge_request_patches

    def _get_review_context(self, merge_request_patches: dict[str, PatchedFile]) -> list[DiscussionReviewContext]:
        """
        Extract discussions data from merge request to be addressed by the agent.

        It will extract the discussions that are not resolved and that have DAIV mentions in the latest note.
        """
        review_context = []

        for discussion in self.client.get_merge_request_review_comments(self.repo_id, self.merge_request_id):
            if not discussion.notes:
                logger.info("Ignoring discussion with no notes: %s", discussion.id)
                continue

            if discussion.notes[-1].author.id == self.client.current_user.id:
                logger.info("Ignoring discussion, DAIV is the current user: %s", discussion.id)
                continue

            if not (note_mentions_daiv(discussion.notes[-1].body, self.client.current_user)):
                logger.info("Ignoring discussion, no DAIV mention in latest note: %s", discussion.id)
                continue

            context = DiscussionReviewContext(discussion=discussion)

            for note in discussion.notes:
                if not note.position:
                    logger.warning("Ignoring note, no position defined: %s", note.id)
                    continue

                path = note.position.new_path or note.position.old_path

                if path not in merge_request_patches:
                    logger.warning("Ignoring note, path not found in patches: %s", note.id)
                    continue

                if context.patch_file is None:
                    # This logic assumes that all notes will have the same patch file
                    context.patch_file = merge_request_patches[path]

                    context.diff = self.note_processor.extract_diff(
                        note, context.patch_file, self._get_file_content(path)
                    )

                context.notes.append(note)

            if context.notes:
                review_context.append(context)
        return review_context

    def _get_comments_context(self, merge_request_patches: dict[str, PatchedFile]) -> DiscussionReviewContext | None:
        """
        Get the comments context from the merge request.
        """

        comments = self.client.get_merge_request_comments(self.repo_id, self.merge_request_id)

        if not discussion_has_daiv_mentions(comments[-1], self.client.current_user):
            logger.info("Ignoring merge request: %s, no DAIV mentions in latest comment.", self.merge_request_id)
            return None

        comment_context = DiscussionReviewContext(
            discussion=comments[-1], diff=merge_request_patches["__all__"].__str__()
        )

        for comment in comments:
            if not comment.notes:
                logger.info("Ignoring comment with no notes: %s", comment.id)
                continue

            comment_context.notes.extend(comment.notes)

        return comment_context

    def _get_file_content(self, path: str) -> str:
        """
        Get the file content from the repository.
        """
        ctx = get_repository_ctx()
        resolved_file_path = (ctx.repo_dir / path).resolve()

        if (
            not resolved_file_path.exists()
            or not resolved_file_path.is_file()
            or any(fnmatch.fnmatch(path, pattern) for pattern in ctx.config.combined_exclude_patterns)
        ):
            raise ValueError(f"File content '{path}' not found")

        return resolved_file_path.read_text()
