from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from django.conf import settings

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.memory import InMemoryStore
from unidiff import LINE_TYPE_CONTEXT, Hunk, PatchedFile, PatchSet
from unidiff.patch import Line

from automation.agents.nodes import apply_format_code
from automation.agents.review_addressor.agent import ReviewAddressorAgent
from automation.agents.review_addressor.conf import settings as review_addressor_settings
from codebase.base import Discussion, Note, NoteDiffPosition, NoteDiffPositionType, NotePositionType, NoteType
from codebase.clients import RepoClient
from codebase.utils import discussion_has_daiv_mentions, notes_to_messages
from core.utils import generate_uuid

from .base import BaseManager

if TYPE_CHECKING:
    from codebase.clients import AllRepoClient

logger = logging.getLogger("daiv.agents")


@dataclass
class DiscussionReviewContext:
    discussion: Discussion
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

    def __init__(self, client: AllRepoClient, repo_id: str, ref: str | None = None, **kwargs):
        super().__init__(client, repo_id, ref)
        self.merge_request_id = kwargs["merge_request_id"]
        self.note_processor = NoteProcessor()

    @classmethod
    async def process_review(cls, repo_id: str, merge_request_id: int, ref: str | None = None):
        """
        Process code review for merge request.
        """
        client = RepoClient.create_instance()
        manager = cls(client, repo_id, ref, merge_request_id=merge_request_id)

        resolved_discussions: list[tuple[str, str]] = []

        for context in manager._process_discussions(manager._extract_merge_request_diffs()):
            try:
                if result := await manager._process_discussion(context):
                    resolved_discussions.append(result)
            except Exception:
                # If there is an error, we will not resolve the discussion but we will continue to process the next one,
                # avoiding loosing the work done so far.
                logger.exception("Error processing discussion: %s", context.discussion.id)

        if await manager._get_file_changes():
            await apply_format_code(repo_id, ref, manager._file_changes_store)
            await manager._commit_changes(file_changes=await manager._get_file_changes())

        for discussion_id, note_id in resolved_discussions:
            manager.client.update_merge_request_discussion_note(
                manager.repo_id,
                manager.merge_request_id,
                discussion_id,
                note_id,
                "✅ Review comment addressed—ready for your check!",
                mark_as_resolved=True,
            )

    async def _process_discussion(self, context: DiscussionReviewContext) -> tuple[str, str] | None:
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
        await self._set_file_changes(await self._get_file_changes(), store=file_changes_store)

        async with AsyncPostgresSaver.from_conn_string(settings.DB_URI) as checkpointer:
            reviewer_addressor = await ReviewAddressorAgent(store=file_changes_store, checkpointer=checkpointer).agent

            current_state = await reviewer_addressor.aget_state(config, subgraphs=True)
            if current_state.created_at is not None:
                # If the thread already exists, we will delete it to avoid conflicts. This is a workaround to avoid
                # conflicts when the same discussion is processed multiple times.
                await checkpointer.adelete_thread(thread_id)

            note_id = self._add_workflow_step_note("start", context.discussion.id)

            try:
                async for event in reviewer_addressor.astream_events(
                    {"notes": notes_to_messages(context.notes, self.client.current_user.id), "diff": context.diff},
                    config,
                    include_names=["plan", "execute_plan"],
                    include_types=["on_chain_start"],
                ):
                    if event["event"] == "on_chain_start":
                        self._add_workflow_step_note(event["name"], context.discussion.id, note_id)
            except Exception:
                logger.exception("Error processing discussion: %s", context.discussion.id)
                self._add_workflow_step_note("error", context.discussion.id, note_id)
                return None

            current_state = await reviewer_addressor.aget_state(config, subgraphs=True)

            if note := (current_state.values.get("reply") or current_state.values.get("plan_questions")):
                self.client.update_merge_request_discussion_note(
                    self.repo_id, self.merge_request_id, context.discussion.id, note_id, note
                )
            elif files_to_commit := await self._get_file_changes(store=file_changes_store):
                # Update the global file changes store with file changes that resulted from the discussion resolution.
                await self._set_file_changes(files_to_commit)
                self._add_workflow_step_note("addressed", context.discussion.id, note_id)
                return context.discussion.id, note_id

        return None

    def _extract_merge_request_diffs(self) -> dict[str, PatchedFile]:
        """
        Extract patch files from merge request.
        """
        merge_request_patches: dict[str, PatchedFile] = {}
        patch_set_all = PatchSet([])

        for mr_diff in self.client.get_merge_request_diff(self.repo_id, self.merge_request_id):
            if mr_diff.diff:
                patch_set = PatchSet.from_string(mr_diff.diff, encoding="utf-8")
                merge_request_patches[patch_set[0].path] = patch_set[0]
                patch_set_all.append(patch_set[0])

        merge_request_patches["__all__"] = patch_set_all
        return merge_request_patches

    def _process_discussions(self, merge_request_patches: dict[str, PatchedFile]) -> list[DiscussionReviewContext]:
        """
        Extract discussions data from merge request to be processed later.
        """
        discussions = []

        for discussion in self.client.get_merge_request_discussions(
            self.repo_id, self.merge_request_id, note_types=[NoteType.DIFF_NOTE, NoteType.DISCUSSION_NOTE]
        ):
            if not discussion.notes:
                logger.info("Ignoring discussion with no notes: %s", discussion.id)
                continue

            if discussion.notes[-1].author.id == self.client.current_user.id:
                logger.info("Ignoring discussion, DAIV is the current user: %s", discussion.id)
                continue

            if not (discussion_has_daiv_mentions(discussion, self.client.current_user)):
                logger.info("Ignoring discussion, no DAIV mention or DAIV notes: %s", discussion.id)
                continue

            context = DiscussionReviewContext(discussion=discussion)

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

                    if context.patch_file is None:
                        # This logic assumes that all notes will have the same patch file
                        context.patch_file = merge_request_patches[path]
                        file_content = self.client.get_repository_file(self.repo_id, path, self.ref)
                        if not file_content:
                            raise ValueError(f"File content '{path}' for note: {note.id} not found")
                        context.diff = self.note_processor.extract_diff(note, context.patch_file, file_content)

                context.notes.append(note)

            if context.notes:
                discussions.append(context)
        return discussions

    def _add_workflow_step_note(
        self,
        step_name: Literal["start", "plan", "execute_plan", "addressed", "error"],
        discussion_id: str,
        note_id: str | None = None,
    ) -> str | None:
        """
        Add a note to the discussion to inform the user that the workflow step has been completed.
        """
        if step_name == "start":
            note_message = "⏳ Addressing your review comment — *in progress* ..."
        if step_name == "plan":
            note_message = "🛠️ Drafting a plan to address the review — *in progress* ..."
        elif step_name == "execute_plan":
            note_message = "🚀 Executing the plan — *in progress* ..."
        elif step_name == "addressed":
            note_message = "✅ Review comment addressed—I'll include the changes shortly with the final updates!"
        elif step_name == "error":
            note_message = "⚠️ I encountered an issue addressing this comment. Reply to this comment to retry!"

        if note_id:
            self.client.update_merge_request_discussion_note(
                self.repo_id, self.merge_request_id, discussion_id, note_id, note_message
            )
        else:
            return self.client.create_merge_request_discussion_note(
                self.repo_id, self.merge_request_id, note_message, discussion_id=discussion_id
            )
