from automation.quick_actions.base import BaseAction, QuickAction, Scope
from automation.quick_actions.decorator import quick_action
from codebase.base import Discussion, Issue, MergeRequest, Note


class EchoAction(BaseAction):
    trigger: str = ""
    description: str = "Echoes back the provided message."


@quick_action(verb="echo", scopes=[Scope.ISSUE, Scope.MERGE_REQUEST])
class EchoQuickAction(QuickAction):
    """
    Echoes back the provided message.
    """

    actions = [EchoAction]

    async def execute_action(
        self,
        repo_id: str,
        *,
        args: str,
        scope: Scope,
        discussion: Discussion,
        note: Note,
        issue: Issue | None = None,
        merge_request: MergeRequest | None = None,
        is_reply: bool = False,
    ) -> None:
        """
        Execute the echo action.

        Args:
            repo_id: The repository ID.
            scope: The scope of the quick action.
            discussion: The discussion that triggered the action.
            note: The note that triggered the action.
            issue: The issue where the action was triggered (if applicable).
            merge_request: The merge request where the action was triggered (if applicable).
            args: Additional parameters from the command.
            is_reply: Whether the action was triggered as a reply.
        """
        # Prepare the echo message
        echo_message = "No message to echo" if not args or args.strip() == "" else f"Echo: {args}"

        # Send response based on scope
        if scope == Scope.ISSUE:
            self.client.create_issue_discussion_note(repo_id, issue.iid, echo_message, discussion.id)
        elif scope == Scope.MERGE_REQUEST:
            self.client.create_merge_request_discussion_note(
                repo_id, merge_request.merge_request_id, echo_message, discussion.id, mark_as_resolved=True
            )
