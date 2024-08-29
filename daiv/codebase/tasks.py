import logging
import textwrap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.core.cache import cache

from celery import shared_task
from redis.exceptions import LockError
from unidiff import Hunk, PatchedFile, PatchSet

from automation.agents.models import Usage
from automation.coders.change_describer.coder import ChangesDescriberCoder
from automation.coders.review_addressor.coder import ReviewAddressorCoder, ReviewCommentorCoder
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
class DiscussionToAdress:
    repo_id: str
    merge_request_id: int
    merge_request_source_branch: str
    discussion: Discussion
    patch_file: PatchedFile | None = None
    notes: list[Note] = field(default_factory=list)
    diff: str | None = None


def locked_task(key: str = ""):
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
def handle_mr_feedback(repo_id: str, merge_request_id: int, merge_request_source_branch: str):
    """
    Handle feedback for a merge request.
    """
    usage = Usage()
    client = RepoClient.create_instance()

    merge_request_patchs: dict[str, PatchedFile] = {}
    discussion_to_address_list: list[DiscussionToAdress] = []

    try:
        for merge_request_diff in client.get_merge_request_diff(repo_id, merge_request_id):
            # Each patch set contains a single file diff (no multiple files in a single MR diff)
            patch_set = PatchSet.from_string(merge_request_diff.diff, encoding="utf-8")
            merge_request_patchs[patch_set[0].path] = patch_set[0]

        for discussion in client.get_merge_request_discussions(repo_id, merge_request_id, note_type=NoteType.DIFF_NOTE):
            discussion_to_address = DiscussionToAdress(
                repo_id=repo_id,
                merge_request_id=merge_request_id,
                merge_request_source_branch=merge_request_source_branch,
                discussion=discussion,
            )
            for note in discussion.notes:
                if not note.position:
                    logger.warning("Ignoring note, no `position` defined: %s", note.id)
                    continue

                path = note.position.new_path or note.position.old_path

                if path not in merge_request_patchs:
                    continue

                discussion_to_address.patch_file = merge_request_patchs[path]

                discussion_to_address.notes.append(note)

                if note.position.position_type == NotePositionType.FILE and not discussion_to_address.diff:
                    discussion_to_address.diff = str(discussion_to_address.patch_file)

                elif (
                    note.position.position_type == NotePositionType.TEXT
                    and note.position.line_range
                    and not discussion_to_address.diff
                ):
                    if (
                        note.position.line_range.start.type == NoteDiffPositionType.EXPANDED
                        or note.position.line_range.end.type == NoteDiffPositionType.EXPANDED
                    ):
                        logger.warning("Ignoring diff note, expanded line range not supported yet: %s", note.id)
                        continue

                    if discussion_to_address.patch_file.is_added_file:
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

                    for patch_hunk in discussion_to_address.patch_file:
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
                            diff_header = "\n".join(str(discussion_to_address.patch_file).splitlines()[:2]) + "\n"
                            discussion_to_address.diff = diff_header + str(hunk)
                            break

            if discussion_to_address.notes:
                discussion_to_address_list.append(discussion_to_address)

        for discussion_to_address in discussion_to_address_list:
            _handle_diff_notes(usage, client, discussion_to_address)

    except Exception as e:
        logger.exception("Error handling merge request feedback: %s", e)
    finally:
        # Delete the lock after the task is completed
        cache.delete(f"{repo_id}:{merge_request_id}")


def _handle_diff_notes(usage: Usage, client: RepoClient, discussion_to_address: DiscussionToAdress):
    feedback = ReviewCommentorCoder(usage=usage).invoke(
        source_repo_id=discussion_to_address.repo_id,
        source_ref=discussion_to_address.merge_request_source_branch,
        file_path=discussion_to_address.patch_file.path,
        notes=discussion_to_address.notes,
        diff=discussion_to_address.diff,
    )

    if feedback.questions:
        client.create_merge_request_discussion_note(
            discussion_to_address.repo_id,
            discussion_to_address.merge_request_id,
            discussion_to_address.discussion.id,
            "\n".join(feedback.questions),
        )

        return  # Do not proceed with the changes if there are questions

    file_changes: list[FileChange] = []

    if feedback.code_changes_needed:
        file_changes = ReviewAddressorCoder(usage=usage).invoke(
            source_repo_id=discussion_to_address.repo_id,
            source_ref=discussion_to_address.merge_request_source_branch,
            file_path=discussion_to_address.patch_file.path,
            notes=discussion_to_address.notes,
            diff=discussion_to_address.diff,
        )

    if not feedback.questions:
        client.resolve_merge_request_discussion(
            discussion_to_address.repo_id, discussion_to_address.merge_request_id, discussion_to_address.discussion.id
        )

    if not file_changes:
        # No changes were made, no need to commit
        return

    changes_description: ChangesDescriber | None = ChangesDescriberCoder(usage).invoke(
        changes=[". ".join(file_change.commit_messages) for file_change in file_changes]
    )
    if changes_description is None:
        raise ValueError("No changes description was generated.")

    client.commit_changes(
        discussion_to_address.repo_id,
        discussion_to_address.merge_request_source_branch,
        changes_description.commit_message,
        file_changes,
    )
    client.comment_merge_request(
        discussion_to_address.repo_id,
        discussion_to_address.merge_request_id,
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
