from automation.quick_actions.base import QuickAction, Scope
from automation.quick_actions.decorator import quick_action
from automation.quick_actions.registry import quick_action_registry
from codebase.api.models import Issue, MergeRequest, Note, User
from codebase.clients import RepoClient


@quick_action(verb="help", scopes=[Scope.ISSUE, Scope.MERGE_REQUEST])
class HelpAction(QuickAction):
    """
    A simple hello world quick action for demonstration purposes.
    """

    @property
    def description(self) -> str:
        """Get the description of the help action."""
        return "Shows the help message"

    def execute(
        self,
        repo_id: str,
        scope: Scope,
        note: Note,
        user: User,
        issue: Issue | None = None,
        merge_request: MergeRequest | None = None,
        args: str | None = None,
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

        Returns:
            A help message.
        """
        client = RepoClient.create_instance()
        current_user = client.current_user

        actions = quick_action_registry.get_actions(scope=scope)
        actions_str = "\n".join([
            f"- `@{current_user.username} {action.verb}` - {action().description}" for action in actions
        ])

        if actions_str and scope == Scope.ISSUE:
            note_message = f"You can trigger quick actions by commenting on this issue:\n{actions_str}"
            client.create_issue_discussion_note(repo_id, issue.iid, note_message, note.discussion_id)

        elif actions_str and scope == Scope.MERGE_REQUEST:
            note_message = f"You can trigger quick actions by commenting on this merge request:\n{actions_str}"
            client.create_merge_request_discussion_note(repo_id, merge_request.iid, note_message, note.discussion_id)
            client.resolve_merge_request_discussion(repo_id, merge_request.iid, note.discussion_id)
