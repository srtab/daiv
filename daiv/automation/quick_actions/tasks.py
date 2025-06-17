import logging

from celery import shared_task

from codebase.clients import RepoClient

from .registry import quick_action_registry

logger = logging.getLogger("daiv.quick_actions")


@shared_task
def execute_quick_action_task(
    repo_id: str,
    action_identifier: str,
    note_data: dict,
    user_data: dict,
    issue_data: dict | None = None,
    merge_request_data: dict | None = None,
    params: str | None = None,
) -> None:
    """
    Execute a quick action asynchronously.

    Args:
        repo_id: The repository ID.
        action_identifier: The identifier of the quick action to execute.
        note_data: The note data that triggered the action.
        user_data: The user data who triggered the action.
        issue_data: The issue data (if applicable).
        merge_request_data: The merge request data (if applicable).
        params: Additional parameters from the command.
    """
    try:
        # Get the quick action class from the registry
        action_class = quick_action_registry.get_action_by_identifier(action_identifier)
        if not action_class:
            logger.error(f"Quick action '{action_identifier}' not found in registry")
            return

        # Instantiate and execute the action
        action = action_class()
        result_message = action.execute(
            repo_id=repo_id,
            note=note_data,
            user=user_data,
            issue=issue_data,
            merge_request=merge_request_data,
            params=params,
        )

        # Post the result as a comment
        client = RepoClient.create_instance()
        if issue_data:
            client.comment_issue(repo_id, issue_data["iid"], result_message)
        elif merge_request_data:
            client.comment_merge_request(repo_id, merge_request_data["iid"], result_message)
        else:
            logger.warning(f"No issue or merge request context for quick action '{action_identifier}'")

        logger.info(f"Successfully executed quick action '{action_identifier}' for repo '{repo_id}'")

    except Exception as e:
        logger.error(f"Error executing quick action '{action_identifier}': {str(e)}", exc_info=True)

        # Try to post error feedback to the user
        try:
            client = RepoClient.create_instance()
            error_message = f"❌ Failed to execute quick action `/{action_identifier}`: {str(e)}"

            if issue_data:
                client.comment_issue(repo_id, issue_data["iid"], error_message)
            elif merge_request_data:
                client.comment_merge_request(repo_id, merge_request_data["iid"], error_message)
        except Exception as feedback_error:
            logger.error(f"Failed to post error feedback: {str(feedback_error)}", exc_info=True)