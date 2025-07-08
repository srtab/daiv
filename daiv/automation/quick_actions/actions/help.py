from automation.quick_actions.base import QuickAction, Scope
from automation.quick_actions.decorator import quick_action
from automation.quick_actions.registry import quick_action_registry
from codebase.base import Discussion, Issue, MergeRequest, Note
from codebase.clients import RepoClient


@quick_action(verb="help", scopes=[Scope.ISSUE, Scope.MERGE_REQUEST])
class HelpQuickAction(QuickAction):
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
        *,
        scope: Scope,
        discussion: Discussion,
        note: Note,
        issue: Issue | None = None,
        merge_request: MergeRequest | None = None,
        args: str | None = None,
    ) -> None:
        """
        Execute the help action.

        Args:
            repo_id: The repository ID.
            scope: The scope of the quick action.
            discussion: The discussion that triggered the action.
            note: The note that triggered the action.
            issue: The issue where the action was triggered (if applicable).
            merge_request: The merge request where the action was triggered (if applicable).
            args: Additional parameters from the command.
        """
        client = RepoClient.create_instance()
        current_user = client.current_user

        actions = quick_action_registry.get_actions(scope=scope)
        actions_str = "\n".join([action.help(current_user.username) for action in actions])

        if actions_str and scope == Scope.ISSUE:
            note_message = f"You can trigger quick actions by commenting on this issue:\n{actions_str}"
            client.create_issue_discussion_note(repo_id, issue.iid, note_message, discussion.id)

        elif actions_str and scope == Scope.MERGE_REQUEST:
            note_message = f"You can trigger quick actions by commenting on this merge request:\n{actions_str}"
            client.create_merge_request_discussion_note(
                repo_id, merge_request.merge_request_id, note_message, discussion.id
            )
            client.resolve_merge_request_discussion(repo_id, merge_request.merge_request_id, discussion.id)
