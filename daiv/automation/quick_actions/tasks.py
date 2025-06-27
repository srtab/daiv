import logging

from asgiref.sync import async_to_sync
from celery import shared_task

from codebase.api.models import Issue, MergeRequest, Note, User
from codebase.clients import RepoClient

from .base import Scope
from .registry import quick_action_registry

logger = logging.getLogger("daiv.quick_actions")


@shared_task(pydantic=True)
def execute_quick_action_task(
    repo_id: str,
    action_verb: str,
    action_scope: str,
    note: Note,
    user: User,
    issue: Issue | None = None,
    merge_request: MergeRequest | None = None,
    action_args: list[str] | None = None,
) -> None:
    """
    Execute a quick action asynchronously.

    Args:
        repo_id: The repository ID.
        action_verb: The verb of the quick action to execute.
        action_scope: The scope of the quick action to execute.
        note: The note data that triggered the action.
        user: The user data who triggered the action.
        issue: The issue data (if applicable).
        merge_request: The merge request data (if applicable).
        action_args: Additional parameters from the command.
    """
    action_classes = quick_action_registry.get_actions(verb=action_verb, scope=Scope(action_scope))

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

    try:
        action = action_classes[0]()
        async_to_sync(action.execute)(
            repo_id=repo_id,
            scope=Scope(action_scope),
            note=note,
            user=user,
            issue=issue,
            merge_request=merge_request,
            args=action_args,
        )

    except Exception as e:
        logger.exception("Error executing quick action '%s' for repo '%s': %s", action_verb, repo_id, str(e))

        error_message = f"❌ Failed to execute quick action `{action_verb}`."

        client = RepoClient.create_instance()
        if issue:
            client.create_issue_discussion_note(repo_id, issue.iid, error_message, note.discussion_id)
        elif merge_request:
            client.create_merge_request_discussion_note(repo_id, merge_request.iid, error_message, note.discussion_id)

    else:
        if issue:
            logger.info(
                "Successfully executed quick action '%s' for repo '%s' on issue '%s'", action_verb, repo_id, issue.iid
            )
        elif merge_request:
            logger.info(
                "Successfully executed quick action '%s' for repo '%s' on merge request '%s'",
                action_verb,
                repo_id,
                merge_request.iid,
            )
