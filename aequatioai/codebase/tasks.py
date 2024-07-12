import logging
import textwrap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from django.core.cache import cache

from celery import shared_task
from redis.exceptions import LockError
from unidiff import Hunk, PatchedFile, PatchSet

from automation.agents.models import Usage
from automation.coders.change_describer.coder import ChangesDescriberCoder
from automation.coders.refactor.coder_simple import SimpleRefactorCoder
from automation.coders.refactor.prompts import RefactorPrompts
from codebase.base import Discussion, FileChange, Note, NoteDiffPositionType, NotePositionType, NoteType
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

if TYPE_CHECKING:
    from unidiff.patch import Line

    from automation.coders.change_describer.models import ChangesDescriber

logger = logging.getLogger(__name__)


@shared_task
def update_index_by_repo_id(repo_ids: list[str], reset: bool = False):
    """
    Update the index of all repositories with the given IDs.
    """
    for repo_id in repo_ids:
        update_index_repository(repo_id, reset)


@shared_task
def update_index_by_topics(topics: list[str], reset: bool = False):
    """
    Update the index of all repositories with the given topics.
    """
    repo_client = RepoClient.create_instance()
    for project in repo_client.list_repositories(topics=topics, load_all=True):
        update_index_repository(project.slug, reset)


@shared_task
def update_index_repository(repo_id: str, reset: bool = False):
    """
    Update codebase index of a repository.
    """
    repo_client = RepoClient.create_instance()
    indexer = CodebaseIndex(repo_client=repo_client)
    if reset:
        indexer.delete(repo_id=repo_id)
    indexer.update(repo_id=repo_id)


@dataclass
class FeedbackToAdress:
    repo_id: str
    merge_request_id: int
    merge_request_source_branch: str
    patch_file: PatchedFile
    discussions: list[Discussion] = field(default_factory=list)
    file_notes: list[Note] = field(default_factory=list)
    text_notes: list[Note] = field(default_factory=list)


def locked_task(key: str):
    def decorator(func):
        def wrapper(*args, **kwargs):
            try:
                with cache.lock(f"{func.__name__}:{key.format(**kwargs)}", blocking=False):
                    func(*args, **kwargs)
            except LockError:
                logger.warning("Task: Ignored task, already processing.")
                return

        return wrapper

    return decorator


@shared_task
@locked_task("{repo_id}:{merge_request_id}")
def handle_mr_feedback(repo_id: str, merge_request_id: int, merge_request_source_branch: str):
    """
    Handle feedback for a merge request.
    """

    usage = Usage()
    client = RepoClient.create_instance()

    feedback_to_address_by_file: dict[str, FeedbackToAdress] = {}

    for merge_request_diff in client.get_merge_request_diff(repo_id, merge_request_id):
        # Each patch set contains a single file diff (no multiple files in a single MR diff)
        patch_set = PatchSet.from_string(merge_request_diff.diff, encoding="utf-8")
        feedback_to_address_by_file[patch_set[0].path] = FeedbackToAdress(
            repo_id=repo_id,
            merge_request_id=merge_request_id,
            merge_request_source_branch=merge_request_source_branch,
            patch_file=patch_set[0],
        )

    for discussion in client.get_merge_request_discussions(repo_id, merge_request_id, note_type=NoteType.DIFF_NOTE):
        for note in discussion.notes:
            if not note.position or not note.position.line_range:
                logger.warning("Ignoring note, no `position` or `line_range` defined: %s", note.id)
                continue

            path = note.position.new_path or note.position.old_path

            if path not in feedback_to_address_by_file:
                continue

            if discussion not in feedback_to_address_by_file[path].discussions:
                feedback_to_address_by_file[path].discussions.append(discussion)

            if note.position.position_type == NotePositionType.FILE:
                feedback_to_address_by_file[path].file_notes.append(note)
            elif note.position.position_type == NotePositionType.TEXT:
                if (
                    note.position.line_range.start.type == NoteDiffPositionType.EXPANDED
                    or note.position.line_range.end.type == NoteDiffPositionType.EXPANDED
                ):
                    logger.warning("Ignoring diff note, expanded line range not supported yet: %s", note.id)
                    continue

                if feedback_to_address_by_file[path].patch_file.is_added_file:
                    pass
                    # TODO: optimized case for added files, no need to find the diff lines

                start_side = "target"
                start_line_no = note.position.line_range.start.new_line
                if note.position.line_range.start.type == NoteDiffPositionType.OLD:
                    start_side = "source"
                    start_line_no = note.position.line_range.start.old_line

                end_side = "target"
                end_line_no = note.position.line_range.end.new_line
                if note.position.line_range.end.type == NoteDiffPositionType.OLD:
                    end_side = "source"
                    end_line_no = note.position.line_range.end.old_line

                for patch_hunk in feedback_to_address_by_file[path].patch_file:
                    start = getattr(patch_hunk, f"{start_side}_start")
                    length = getattr(patch_hunk, f"{start_side}_length")
                    if start <= start_line_no <= start + length:
                        diff_code_lines: list[Line] = []

                        for patch_line in patch_hunk:
                            start_side_line_no = getattr(patch_line, f"{start_side}_line_no")
                            end_side_line_no = getattr(patch_line, f"{end_side}_line_no")

                            if (
                                (start_side_line_no and start_side_line_no >= start_line_no)
                                # we need to check diff_code_lines here to only check the end_line_no after we have
                                # found the start_line_no.
                                # Otherwise, we might end up with a line that is not part of the diff code lines.
                                or (diff_code_lines and end_side_line_no and end_side_line_no <= end_line_no)
                            ):
                                diff_code_lines.append(patch_line)

                            if end_side_line_no and end_line_no == end_side_line_no:
                                break

                        hunk = Hunk(
                            src_start=diff_code_lines[0].source_line_no or diff_code_lines[0].target_line_no,
                            src_len=len([line for line in diff_code_lines if line.is_context or line.is_removed]),
                            tgt_start=diff_code_lines[0].target_line_no or diff_code_lines[0].source_line_no,
                            tgt_len=len([line for line in diff_code_lines if line.is_context or line.is_added]),
                        )
                        hunk.extend(diff_code_lines)
                        note.hunk = str(hunk)
                        feedback_to_address_by_file[path].text_notes.append(note)

    for feedback_to_address in feedback_to_address_by_file.values():
        _handle_diff_notes(usage, client, feedback_to_address)


def _handle_diff_notes(usage: Usage, client: RepoClient, feedback_to_address: FeedbackToAdress):
    changes: list[FileChange] = []

    if feedback_to_address.file_notes:
        changes = SimpleRefactorCoder(usage=usage).invoke(
            prompt=RefactorPrompts.format_file_review_feedback_prompt(
                feedback_to_address.patch_file.path, [note.body for note in feedback_to_address.file_notes]
            ),
            source_repo_id=feedback_to_address.repo_id,
            source_ref=feedback_to_address.merge_request_source_branch,
        )

    if feedback_to_address.text_notes:
        changes = SimpleRefactorCoder(usage=usage).invoke(
            prompt=RefactorPrompts.format_diff_review_feedback_prompt(
                feedback_to_address.patch_file.path,
                [(note.body, cast(str, note.hunk)) for note in feedback_to_address.text_notes],
            ),
            source_repo_id=feedback_to_address.repo_id,
            source_ref=feedback_to_address.merge_request_source_branch,
        )

    if not changes:
        return

    changes_description: ChangesDescriber | None = ChangesDescriberCoder(usage).invoke(
        changes=[". ".join(file_change.commit_messages) for file_change in changes]
    )
    if changes_description is None:
        raise ValueError("No changes description was generated.")

    for discussion in feedback_to_address.discussions:
        client.resolve_merge_request_discussion(
            feedback_to_address.repo_id, feedback_to_address.merge_request_id, discussion.id
        )

    client.commit_changes(
        feedback_to_address.repo_id,
        feedback_to_address.merge_request_source_branch,
        changes_description.commit_message,
        changes,
    )
    client.comment_merge_request(
        feedback_to_address.repo_id,
        feedback_to_address.merge_request_id,
        textwrap.dedent(
            """\
                I've made the changes: **{changes}**.

                Please review them and let me know if you need further assistance.

                ### 🤓 Stats for the nerds:
                Prompt tokens: **{prompt_tokens:,}** \\
                Completion tokens: **{completion_tokens:,}** \\
                Total tokens: **{total_tokens:,}** \\
                Estimated cost: **${total_cost:.10f}**"""
        ).format(
            changes=changes_description.title,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            total_cost=usage.cost,
        ),
    )
