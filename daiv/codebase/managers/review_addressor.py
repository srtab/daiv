from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.conf import settings

from langchain_community.callbacks import get_openai_callback
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import START
from unidiff import Hunk, PatchedFile, PatchSet

from automation.agents.pr_describer.agent import PullRequestDescriberAgent
from automation.agents.review_addressor.agent import ReviewAddressorAgent
from codebase.base import (
    Discussion,
    FileChange,
    Note,
    NoteDiffPosition,
    NoteDiffPositionType,
    NotePositionType,
    NoteType,
)
from codebase.clients import RepoClient
from codebase.utils import notes_to_messages
from core.config import RepositoryConfig

if TYPE_CHECKING:
    from unidiff.patch import Line

    from codebase.clients import AllRepoClient

logger = logging.getLogger(__name__)


@dataclass
class DiscussionReviewContext:
    discussion: Discussion
    patch_file: PatchedFile | None = None
    notes: list[Note] = field(default_factory=list)
    diff: str | None = None


class DiffProcessor:
    """Abstract base class for processing diffs."""

    def process_diff(self, diff_content: bytes) -> PatchedFile:
        patch_set = PatchSet.from_string(diff_content, encoding="utf-8")
        return patch_set[0] if patch_set else None


class NoteProcessor:
    """
    Processes text-based diff notes.
    """

    def extract_diff(self, note: Note, patch_file: PatchedFile) -> str | None:
        """
        Extract diff content where the note was left.
        """
        if not note.position or not note.position.line_range:
            return None

        if note.position.position_type == NotePositionType.FILE:
            return str(patch_file)
        elif note.position.position_type == NotePositionType.TEXT:
            return self._extract_diff_content(note, patch_file)

    def _extract_diff_content(self, note: Note, patch_file: PatchedFile) -> str | None:
        """
        Extract diff content from note.
        """
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


class ReviewAddressorManager:
    """
    Manages the code review process.
    """

    def __init__(self, client: AllRepoClient, repo_id: str, merge_request_source_branch: str):
        self.client = client
        self.repo_id = repo_id
        self.merge_request_source_branch = merge_request_source_branch
        self.repo_config = RepositoryConfig.get_config(repo_id=repo_id)
        self.diff_processor = DiffProcessor()
        self.note_processor = NoteProcessor()

    @classmethod
    def process_review(cls, repo_id: str, merge_request_id: int, merge_request_source_branch: str):
        """
        Process code review for merge request.
        """
        client = RepoClient.create_instance()
        manager = cls(client, repo_id, merge_request_source_branch)
        manager._process_review(merge_request_id)

    def _process_review(self, merge_request_id: int):
        merge_request_patches = self._extract_merge_request_diffs(merge_request_id)
        for context in self._process_discussions(merge_request_id, merge_request_patches):
            config = RunnableConfig(
                configurable={"thread_id": f"{self.repo_id}!{merge_request_id}#{context.discussion.id}"}
            )

            with (
                PostgresSaver.from_conn_string(settings.DB_URI) as checkpointer,
                get_openai_callback() as usage_handler,
            ):
                reviewer_addressor = ReviewAddressorAgent(
                    self.client,
                    source_repo_id=self.repo_id,
                    source_ref=self.merge_request_source_branch,
                    merge_request_id=merge_request_id,
                    discussion_id=context.discussion.id,
                    usage_handler=usage_handler,
                    checkpointer=checkpointer,
                )
                reviewer_addressor_agent = reviewer_addressor.agent

                current_state = reviewer_addressor_agent.get_state(config)

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
                    result = reviewer_addressor_agent.invoke(None, config)

                if "response" in result:
                    self.client.create_merge_request_discussion_note(
                        self.repo_id, merge_request_id, context.discussion.id, result["response"]
                    )

                if file_changes := reviewer_addressor.get_files_to_commit():
                    self._commit_changes(
                        merge_request_id=merge_request_id,
                        discussion_id=context.discussion.id,
                        file_changes=file_changes,
                    )

    def _extract_merge_request_diffs(self, merge_request_id: int):
        """
        Extract patch files from merge request.
        """
        merge_request_patches: dict[str, PatchedFile] = {}

        for mr_diff in self.client.get_merge_request_diff(self.repo_id, merge_request_id):
            if mr_diff.diff:
                patch_set = PatchSet.from_string(mr_diff.diff, encoding="utf-8")
                merge_request_patches[patch_set[0].path] = patch_set[0]

        return merge_request_patches

    def _process_discussions(
        self, merge_request_id: int, merge_request_patches: dict[str, PatchedFile]
    ) -> list[DiscussionReviewContext]:
        """
        Process discussions from merge request.
        """
        discussions = []

        for discussion in self.client.get_merge_request_discussions(
            self.repo_id, merge_request_id, note_type=NoteType.DIFF_NOTE
        ):
            if discussion.notes[-1].author.id == self.client.current_user.id:
                logger.debug("Ignoring discussion, DAIV is the current user: %s", discussion.id)
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
                    # This logic assumes that all notes will have the same patch file and
                    context.patch_file = merge_request_patches[path]
                    context.diff = self.note_processor.extract_diff(note, context.patch_file)

                context.notes.append(note)

            if context.notes:
                discussions.append(context)
        return discussions

    def _commit_changes(self, *, merge_request_id: int, discussion_id: str, file_changes: list[FileChange]):
        """
        Commit changes to the merge request.
        """
        pr_describer = PullRequestDescriberAgent()
        changes_description = pr_describer.agent.invoke({
            "changes": file_changes,
            "branch_name_convention": self.repo_config.branch_name_convention,
        })

        self.client.create_merge_request_discussion_note(
            self.repo_id, merge_request_id, discussion_id, changes_description.description
        )
        self.client.commit_changes(
            self.repo_id, self.merge_request_source_branch, changes_description.commit_message, file_changes
        )
        self.client.resolve_merge_request_discussion(self.repo_id, merge_request_id, discussion_id)
