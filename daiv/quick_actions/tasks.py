import logging

from langchain_core.prompts import jinja2_formatter

from codebase.clients import RepoClient
from daiv import async_task

from .base import Scope
from .registry import quick_action_registry
from .templates import QUICK_ACTION_ERROR_MESSAGE

logger = logging.getLogger("daiv.quick_actions")


@async_task(pydantic=True)
async def execute_issue_task(repo_id: str, action_command: str, action_args: str, comment_id: str, issue_id: int):
    """
    Execute a quick action asynchronously.

    Args:
        repo_id: The repository ID.
        action_command: The command of the quick action to execute.
        action_args: Additional parameters from the command.
        comment_id: The ID of the comment to execute the action on.
        issue_id: The ID of the issue to execute the action on.
    """
    action_classes = quick_action_registry.get_actions(command=action_command, scope=Scope.ISSUE)

    if not action_classes:
        logger.error("Quick action '%s' not found in registry for scope '%s'", action_command, Scope.ISSUE)
        return

    if len(action_classes) > 1:
        logger.error(
            "Multiple quick actions found for '%s' in registry for scope '%s': %s",
            action_command,
            Scope.ISSUE,
            [a.command for a in action_classes],
        )
        return

    client = RepoClient.create_instance()

    comment = client.get_issue_comment(repo_id, issue_id, comment_id)
    issue = client.get_issue(repo_id, issue_id)

    try:
        action = action_classes[0]()
        await action.execute_for_issue(repo_id=repo_id, args=action_args, comment=comment, issue=issue)
    except Exception as e:
        logger.exception("Error executing quick action '%s' for repo '%s': %s", action_command, repo_id, str(e))

        error_message = jinja2_formatter(
            QUICK_ACTION_ERROR_MESSAGE,
            command=f"@{client.current_user.username} /{action_command} {action_args}".strip(),
        )

        client.create_issue_comment(repo_id, issue_id, error_message)
    else:
        logger.info(
            "Successfully executed quick action '%s' for repo '%s' on issue '%s'", action_command, repo_id, issue_id
        )


@async_task(pydantic=True)
async def execute_merge_request_task(
    repo_id: str, action_command: str, action_args: str, comment_id: str, merge_request_id: int
) -> None:
    """
    Execute a quick action asynchronously.

    Args:
        repo_id: The repository ID.
        action_command: The command of the quick action to execute.
        action_args: Additional parameters from the command.
        comment_id: The ID of the comment to execute the action on.
        merge_request_id: The ID of the merge request to execute the action on (if applicable).
    """
    action_classes = quick_action_registry.get_actions(command=action_command, scope=Scope.MERGE_REQUEST)

    if not action_classes:
        logger.error("Quick action '%s' not found in registry for scope '%s'", action_command, Scope.MERGE_REQUEST)
        return

    if len(action_classes) > 1:
        logger.error(
            "Multiple quick actions found for '%s' in registry for scope '%s': %s",
            action_command,
            Scope.MERGE_REQUEST,
            [a.command for a in action_classes],
        )
        return

    client = RepoClient.create_instance()

    comment = client.get_merge_request_comment(repo_id, merge_request_id, comment_id)
    merge_request = client.get_merge_request(repo_id, merge_request_id)

    try:
        action = action_classes[0]()
        await action.execute_for_merge_request(
            repo_id=repo_id, args=action_args, comment=comment, merge_request=merge_request
        )
    except Exception as e:
        logger.exception("Error executing quick action '%s' for repo '%s': %s", action_command, repo_id, str(e))

        error_message = jinja2_formatter(
            QUICK_ACTION_ERROR_MESSAGE,
            command=f"@{client.current_user.username} /{action_command} {action_args}".strip(),
        )

        client.create_merge_request_comment(repo_id, merge_request.merge_request_id, error_message)
    else:
        logger.info(
            "Successfully executed quick action '%s' for repo '%s' on merge request '%s'",
            action_command,
            repo_id,
            merge_request.merge_request_id,
        )
