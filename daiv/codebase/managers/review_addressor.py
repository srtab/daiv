from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.conf import settings

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import START
from unidiff import Hunk, PatchedFile, PatchSet

from automation.agents.pr_describer.agent import PullRequestDescriberAgent
from automation.agents.review_addressor.agent import ReviewAddressorAgent
from codebase.base import (
    Discussion,
    FileChange,
    FileChangeAction,
    Note,
    NoteDiffPosition,
    NoteDiffPositionType,
    NotePositionType,
    NoteType,
)
from codebase.clients import RepoClient
from codebase.managers.base import BaseManager
from codebase.utils import notes_to_messages
from core.utils import generate_uuid

if TYPE_CHECKING:
    from unidiff.patch import Line

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

    def extract_diff(self, note: Note, patch_file: PatchedFile) -> str | None:
        """
        Extract diff content where the note was left.
        """
        if not note.position:
            return None

        if note.position.position_type == NotePositionType.FILE:
            return str(patch_file)
        elif note.position.position_type == NotePositionType.TEXT and note.position.line_range:
            return self._extract_diff_content(note, patch_file)

        return None

    def _extract_diff_content(self, note: Note, patch_file: PatchedFile) -> str | None:
        """
        Extract diff content from note.

        Args:
            note: The note containing position information
            patch_file: The patch file to extract content from

        Returns:
            str | None: The extracted diff content or None if extraction fails
        """
        # Add null safety checks
        if not note.position or not note.position.line_range:
            return None

        if (
            note.position.line_range.start.type == NoteDiffPositionType.EXPANDED
            or note.position.line_range.end.type == NoteDiffPositionType.EXPANDED
        ):
            logger.warning("Ignoring note, expanded line range not supported yet: %s", note.id)
            return None

        # Extract line range information
        start_info = self._get_line_info(note.position.line_range.start)
        end_info = self._get_line_info(note.position.line_range.end)

        return self._build_diff_content(patch_file, start_info, end_info)

    def _get_line_info(self, position: NoteDiffPosition) -> dict:
        """
        Extract line information from position.
        """
        side = "target" if position.type != NoteDiffPositionType.OLD else "source"
        line_no = position.new_line if side == "target" else position.old_line
        return {"side": side, "line_no": line_no}

    def _build_diff_content(self, patch_file: PatchedFile, start_info: dict, end_info: dict) -> str | None:
        """
        Build diff content from patch file based on note position.
        """
        for patch_hunk in patch_file:
            start = getattr(patch_hunk, f"{start_info['side']}_start")
            length = getattr(patch_hunk, f"{start_info['side']}_length")

            if start <= start_info["line_no"] <= start + length:
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
                # Return the diff header and the new hunk
                return diff_header + str(hunk)
        return None


class UniqueFileChangeList:
    """
    A list-like container that maintains uniqueness of FileChange objects based on their file_path and action.
    Later changes to the same file override earlier ones.
    """

    def __init__(self):
        self._changes: dict[tuple[str, FileChangeAction], FileChange] = {}

    def extend(self, changes: list[FileChange]):
        """
        Add multiple FileChange objects, maintaining uniqueness.

        Args:
            changes: A list of FileChange objects to add.
        """
        for change in changes:
            self._changes[(change.file_path, change.action)] = change

    def __bool__(self):
        return bool(self._changes)

    def to_list(self) -> list[FileChange]:
        """
        Convert to a regular list of FileChange objects.

        Returns:
            A list of FileChange objects.
        """
        return list(self._changes.values())


class ReviewAddressorManager(BaseManager):
    """
    Manages the code review process.
    """

    def __init__(self, client: AllRepoClient, repo_id: str, ref: str | None = None, **kwargs):
        super().__init__(client, repo_id, ref)
        self.merge_request_id = kwargs["merge_request_id"]
        self.note_processor = NoteProcessor()

    @classmethod
    def process_review(cls, repo_id: str, merge_request_id: int, ref: str | None = None):
        """
        Process code review for merge request.
        """
        client = RepoClient.create_instance()
        manager = cls(client, repo_id, ref, merge_request_id=merge_request_id)
        manager._process_review()

    def _process_review(self):
        """
        Process code review discussions for merge request.

        If the discussion is resolved, it will save the file changes to be committed later.
        Each iteration of dicussions resolution will be processed with the changes from the previous iterations,
        ensuring that the file changes are processed correctly.
        """
        file_changes = UniqueFileChangeList()
        resolved_discussions: list[str] = []

        merge_request_patches = self._extract_merge_request_diffs()
        for context in self._process_discussions(merge_request_patches):
            thread_id = generate_uuid(f"{self.repo_id}{self.ref}{self.merge_request_id}{context.discussion.id}")

            config = RunnableConfig(configurable={"thread_id": thread_id})

            with PostgresSaver.from_conn_string(settings.DB_URI) as checkpointer:
                reviewer_addressor = ReviewAddressorAgent(
                    self.client,
                    source_repo_id=self.repo_id,
                    source_ref=self.ref,
                    merge_request_id=self.merge_request_id,
                    discussion_id=context.discussion.id,
                    checkpointer=checkpointer,
                    file_changes=file_changes.to_list(),
                )
                reviewer_addressor_agent = reviewer_addressor.agent

                current_state = reviewer_addressor_agent.get_state(config)
                result = None

                # ``current_state.next`` is empty on first run or when the graph is in a final state.
                if not current_state.next or START in current_state.next:
                    result = reviewer_addressor_agent.invoke(
                        {
                            "diff": context.diff,
                            "messages": notes_to_messages(context.notes, self.client.current_user.id),
                            "response": None,
                        },
                        config,
                    )

                elif "human_feedback" in current_state.next:
                    reviewer_addressor_agent.update_state(
                        config,
                        {"messages": notes_to_messages(context.notes, self.client.current_user.id), "response": None},
                        as_node="human_feedback",
                    )

                if current_state.tasks:
                    # This can happen if the agent got an error and we need to retry, or was interrupted.
                    result = reviewer_addressor_agent.invoke(None, config)

                state_after_run = reviewer_addressor_agent.get_state(config)

                if result and "response" in result and result["response"]:
                    self.client.create_merge_request_discussion_note(
                        self.repo_id, self.merge_request_id, result["response"], context.discussion.id
                    )

                if not state_after_run.tasks:
                    if files_to_commit := reviewer_addressor.get_files_to_commit():
                        # Add new file changes while maintaining uniqueness
                        file_changes.extend(files_to_commit)

                    if result and ("response" not in result or not result["response"]):
                        # If the response is not in the result or is empty, it means the discussion was resolved,
                        # no further action would be needed.
                        resolved_discussions.append(context.discussion.id)

        if file_changes:
            self._commit_changes(file_changes=file_changes.to_list(), thread_id=thread_id)

        if resolved_discussions:
            for discussion_id in resolved_discussions:
                self.client.resolve_merge_request_discussion(self.repo_id, self.merge_request_id, discussion_id)

    def _extract_merge_request_diffs(self):
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
            if (last_note := discussion.notes[-1]) and last_note.author.id == self.client.current_user.id:
                logger.debug("Ignoring discussion, DAIV is the current user: %s", discussion.id)
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
                        # This logic assumes that all notes will have the same patch file and
                        context.patch_file = merge_request_patches[path]
                        context.diff = self.note_processor.extract_diff(note, context.patch_file)

                context.notes.append(note)

            if context.notes:
                discussions.append(context)
        return discussions

    def _commit_changes(self, *, file_changes: list[FileChange], thread_id: str):
        """
        Commit changes to the merge request.
        """
        pr_describer = PullRequestDescriberAgent()
        changes_description = pr_describer.agent.invoke(
            {"changes": file_changes, "branch_name_convention": self.repo_config.branch_name_convention},
            config=RunnableConfig(configurable={"thread_id": thread_id}),
        )

        self.client.commit_changes(self.repo_id, self.ref, changes_description.commit_message, file_changes)
