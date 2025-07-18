import logging

from asgiref.sync import async_to_sync
from celery import shared_task
from langchain_core.prompts import jinja2_formatter

from codebase.clients import RepoClient

from .base import Scope
from .registry import quick_action_registry
from .templates import QUICK_ACTION_ERROR_MESSAGE

logger = logging.getLogger("daiv.quick_actions")


@shared_task(pydantic=True)
def execute_quick_action_task(
    repo_id: str,
    action_verb: str,
    action_scope: str,
    discussion_id: str,
    note_id: int,
    issue_id: int | None = None,
    merge_request_id: int | None = None,
    action_args: str | None = None,
) -> None:
    """
    Execute a quick action asynchronously.

    Args:
        repo_id: The repository ID.
        action_verb: The verb of the quick action to execute.
        action_scope: The scope of the quick action to execute.
        discussion_id: The ID of the discussion to execute the action on.
        note_id: The ID of the note to execute the action on.
        issue_id: The ID of the issue to execute the action on (if applicable).
        merge_request_id: The ID of the merge request to execute the action on (if applicable).
        action_args: Additional parameters from the command.
    """
    assert issue_id is not None or merge_request_id is not None, "Either issue_id or merge_request_id must be provided"

    action_scope = Scope(action_scope)
    action_classes = quick_action_registry.get_actions(verb=action_verb, scope=action_scope)

    if not action_classes:
        logger.error("Quick action '%s' not found in registry for scope '%s'", action_verb, action_scope)
        return

    if len(action_classes) > 1:
        logger.error(
            "Multiple quick actions found for '%s' in registry for scope '%s': %s",
            action_verb,
            action_scope,
            [a.verb for a in action_classes],
        )
        return

    client = RepoClient.create_instance()

    issue = None
    merge_request = None
    discussion = None

    if action_scope == Scope.ISSUE:
        discussion = client.get_issue_discussion(repo_id, issue_id, discussion_id, only_resolvable=False)
        issue = client.get_issue(repo_id, issue_id)
    elif action_scope == Scope.MERGE_REQUEST:
        discussion = client.get_merge_request_discussion(
            repo_id, merge_request_id, discussion_id, only_resolvable=False
        )
        merge_request = client.get_merge_request(repo_id, merge_request_id)

    if len(discussion.notes) > 1 and not action_classes[0].can_reply:
        logger.info(
            "Quick action '%s' ignored. It is a reply to a previous note and it's not supported for this action.",
            action_verb,
        )
        return

    try:
        action = action_classes[0]()
        async_to_sync(action.execute)(
            repo_id=repo_id,
            scope=action_scope,
            discussion=discussion,
            note=next(note for note in discussion.notes if note.id == note_id),
            issue=issue,
            merge_request=merge_request,
            args=action_args,
        )
    except Exception as e:
        logger.exception("Error executing quick action '%s' for repo '%s': %s", action_verb, repo_id, str(e))

        error_message = jinja2_formatter(
            QUICK_ACTION_ERROR_MESSAGE, command=f"@{client.current_user.username} {action_verb} {action_args}"
        )

        if action_scope == Scope.ISSUE:
            client.create_issue_discussion_note(repo_id, issue.iid, error_message, discussion.id)
        elif action_scope == Scope.MERGE_REQUEST:
            client.create_merge_request_discussion_note(
                repo_id, merge_request.merge_request_id, error_message, discussion.id
            )
    else:
        if action_scope == Scope.ISSUE:
            logger.info(
                "Successfully executed quick action '%s' for repo '%s' on issue '%s'", action_verb, repo_id, issue.iid
            )
        elif action_scope == Scope.MERGE_REQUEST:
            logger.info(
                "Successfully executed quick action '%s' for repo '%s' on merge request '%s'",
                action_verb,
                repo_id,
                merge_request.merge_request_id,
            )
