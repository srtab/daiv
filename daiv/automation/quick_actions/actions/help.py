from automation.quick_actions.base import QuickAction, Scope
from automation.quick_actions.decorator import quick_action
from automation.quick_actions.registry import quick_action_registry
from codebase.api.models import Issue, MergeRequest, Note, User
from codebase.clients import RepoClient


@quick_action(verb="help", scopes=[Scope.ISSUE, Scope.MERGE_REQUEST])
class HelpAction(QuickAction):
    """
    Shows the help message for the available quick actions.
    """

    @staticmethod
    def description() -> str:
        """Get the description of the help action."""
        return "Shows the help message with the available quick actions."

    async def execute(
        self,
        repo_id: str,
        scope: Scope,
        note: Note,
        user: User,
        issue: Issue | None = None,
        merge_request: MergeRequest | None = None,
        args: list[str] | None = None,
    ) -> None:
        """
        Execute the help action.

        Args:
            repo_id: The repository ID.
            scope: The scope of the quick action.
            note: The note data that triggered the action.
            user: The user who triggered the action.
            issue: The issue data (if applicable).
            merge_request: The merge request data (if applicable).
            args: Additional parameters from the command.
        """
        client = RepoClient.create_instance()
        current_user = client.current_user

        actions = quick_action_registry.get_actions(scope=scope)
        actions_str = "\n".join([action.help(current_user.username) for action in actions])

        if actions_str and scope == Scope.ISSUE:
            note_message = f"You can trigger quick actions by commenting on this issue:\n{actions_str}"
            client.create_issue_discussion_note(repo_id, issue.iid, note_message, note.discussion_id)

        elif actions_str and scope == Scope.MERGE_REQUEST:
            note_message = f"You can trigger quick actions by commenting on this merge request:\n{actions_str}"
            client.create_merge_request_discussion_note(repo_id, merge_request.iid, note_message, note.discussion_id)
            client.resolve_merge_request_discussion(repo_id, merge_request.iid, note.discussion_id)
