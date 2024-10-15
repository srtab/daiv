import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.cache import cache

from celery import shared_task
from langchain_community.callbacks import get_openai_callback
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import START
from unidiff import Hunk, PatchedFile, PatchSet

from automation.graphs.issue_addressor.agent import IssueAddressorAgent
from automation.graphs.issue_addressor.templates import ISSUE_PLANNING_TEMPLATE, ISSUE_REVIEW_PLAN_TEMPLATE
from automation.graphs.review_addressor.agent import ReviewAddressorAgent
from codebase.base import Discussion, Issue, IssueType, Note, NoteDiffPositionType, NotePositionType, NoteType
from codebase.clients import AllRepoClient, RepoClient
from codebase.indexes import CodebaseIndex
from core.constants import BOT_LABEL, BOT_NAME

if TYPE_CHECKING:
    from unidiff.patch import Line


logger = logging.getLogger("daiv.tasks")


def notes_to_messages(notes: list[Note], bot_user_id) -> list[AnyMessage]:
    """
    Convert a list of notes to a list of messages.
    """
    messages = []
    for note in notes:
        if note.author.id == bot_user_id:
            messages.append(AIMessage(content=note.body, name=BOT_NAME))
        else:
            messages.append(HumanMessage(content=note.body, name=note.author.username))
    return messages


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
def update_index_repository(repo_id: str, ref: str | None = None, reset: bool = False):
    """
    Update codebase index of a repository.
    """
    repo_client = RepoClient.create_instance()
    indexer = CodebaseIndex(repo_client=repo_client)
    if reset:
        indexer.delete(repo_id=repo_id, ref=ref)
    indexer.update(repo_id=repo_id, ref=ref)


@shared_task
def address_issue_task(
    repo_id: str, ref: str, issue_iid: int, should_reset_plan: bool = False, cache_key: str | None = None
):
    """
    Address an issue by creating a merge request with the changes described on the issue description.
    """
    try:
        client = RepoClient.create_instance()
        issue = client.get_issue(repo_id, issue_iid)

        # Check if the issue already has a comment from the bot. If it doesn't, we need to add one.
        if not next((note.body for note in issue.notes if note.author.id == client.current_user.id), None):
            client.comment_issue(
                repo_id, issue.iid, ISSUE_PLANNING_TEMPLATE.format(assignee=issue.assignee.username, bot_name=BOT_LABEL)
            )

        config = {"configurable": {"thread_id": f"{repo_id}#{issue_iid}"}}

        with PostgresSaver.from_conn_string(settings.DB_URI) as checkpointer:
            issue_addressor = IssueAddressorAgent(
                client, source_repo_id=repo_id, source_ref=ref, issue_id=issue_iid, checkpointer=checkpointer
            ).agent

            if should_reset_plan and (history_states := list(issue_addressor.get_state_history(config))):
                config = history_states[-1].config  # Replay the first state to reset the plan, just like a reset

            current_state = issue_addressor.get_state(config)

            if START in current_state.next:
                result = issue_addressor.invoke(
                    {"issue_title": issue.title, "issue_description": issue.description}, config
                )

                if result["request_for_changes"] and result["plan_tasks"]:
                    # Delete all the tasks before creating new ones
                    for issue_tasks in client.get_issue_tasks(repo_id, issue.iid):
                        client.delete_issue(repo_id, issue_tasks.id)

                    # Create the new tasks
                    issue_tasks = [
                        Issue(
                            title=plan_task, assignee=client.current_user, issue_type=IssueType.TASK, labels=[BOT_LABEL]
                        )
                        for plan_task in result["plan_tasks"]
                    ]
                    client.create_issue_tasks(repo_id, issue.id, issue_tasks)

                    # Request the reporter to review the plan
                    client.comment_issue(repo_id, issue.iid, ISSUE_REVIEW_PLAN_TEMPLATE)

                elif not result["request_for_changes"]:
                    client.comment_issue(repo_id, issue.iid, "No tasks were planned.")

            elif "human_feedback" in current_state.next:
                if discussions := list(client.get_issue_discussions(repo_id, issue.iid)):
                    issue_addressor.update_state(
                        config, {"messages": notes_to_messages(discussions[-1].notes, client.current_user.id)}
                    )
                    result = issue_addressor.invoke(None, config)
                    if result["response"]:
                        client.comment_issue(repo_id, issue.iid, result["response"])
                else:
                    client.comment_issue(repo_id, issue.iid, "No feedback was provided.")

    except Exception as e:
        logger.exception("Error handling issue: %s", e)
    finally:
        if cache_key:
            # Delete the lock after the task is completed
            cache.delete(cache_key)


@dataclass
class DiscussionToAdress:
    repo_id: str
    merge_request_id: int
    merge_request_source_branch: str
    discussion: Discussion
    patch_file: PatchedFile | None = None
    notes: list[Note] = field(default_factory=list)
    diff: str | None = None


@shared_task
def handle_mr_feedback(repo_id: str, merge_request_id: int, merge_request_source_branch: str, cache_key: str):
    """
    Handle feedback for a merge request.
    """
    client = RepoClient.create_instance()

    merge_request_patchs: dict[str, PatchedFile] = {}
    discussion_to_address_list: list[DiscussionToAdress] = []

    try:
        for merge_request_diff in client.get_merge_request_diff(repo_id, merge_request_id):
            # Each patch set contains a single file diff (no multiple files in a single MR diff)
            patch_set = PatchSet.from_string(merge_request_diff.diff, encoding="utf-8")
            merge_request_patchs[patch_set[0].path] = patch_set[0]

        for discussion in client.get_merge_request_discussions(repo_id, merge_request_id, note_type=NoteType.DIFF_NOTE):
            if discussion.notes[-1].author.id == client.current_user.id:
                # Skip the discussion if the last note was made by the bot
                continue

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
                                    or (
                                        diff_code_lines
                                        and (
                                            end_side_line_no is None
                                            or end_side_line_no
                                            and end_side_line_no <= end_line_no
                                        )
                                    )
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
            _handle_diff_notes(client, discussion_to_address)

    except Exception as e:
        logger.exception("Error handling merge request feedback: %s", e)
    finally:
        # Delete the lock after the task is completed
        cache.delete(cache_key)


def _handle_diff_notes(client: AllRepoClient, discussion_to_address: DiscussionToAdress):
    messages: list[AnyMessage] = []

    for note in discussion_to_address.notes:
        if note.author.id == client.current_user.id:
            messages.append(AIMessage(content=note.body, name=BOT_NAME))
        else:
            messages.append(HumanMessage(content=note.body, name=note.author.username))

    with get_openai_callback() as usage_handler:
        reviewer_agent = ReviewAddressorAgent(
            client,
            source_repo_id=discussion_to_address.repo_id,
            source_ref=discussion_to_address.merge_request_source_branch,
            merge_request_id=discussion_to_address.merge_request_id,
            discussion_id=discussion_to_address.discussion.id,
            usage_handler=usage_handler,
        )

        reviewer_agent.agent.invoke({"diff": discussion_to_address.diff, "messages": messages})
