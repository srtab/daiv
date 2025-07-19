from langchain_core.prompts.string import jinja2_formatter

from automation.quick_actions.base import BaseAction, QuickAction, Scope
from automation.quick_actions.decorator import quick_action
from automation.quick_actions.registry import quick_action_registry
from automation.quick_actions.templates import QUICK_ACTIONS_TEMPLATE
from codebase.base import Discussion, Issue, MergeRequest, Note
from core.constants import BOT_NAME


class HelpAction(BaseAction):
    trigger: str = ""
    description: str = "Shows the help message with the available quick actions."


@quick_action(verb="help", scopes=[Scope.ISSUE, Scope.MERGE_REQUEST])
class HelpQuickAction(QuickAction):
    """
    Shows the help message for the available quick actions.
    """

    actions = [HelpAction]

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
        Execute the help action.

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
        actions = quick_action_registry.get_actions(scope=scope)
        if actions_help := [action.help(self.client.current_user.username, is_reply=is_reply) for action in actions]:
            note_message = jinja2_formatter(
                QUICK_ACTIONS_TEMPLATE, bot_name=BOT_NAME, scope=scope, actions=actions_help
            )
            if scope == Scope.ISSUE:
                self.client.create_issue_discussion_note(repo_id, issue.iid, note_message, discussion.id)

            elif scope == Scope.MERGE_REQUEST:
                self.client.create_merge_request_discussion_note(
                    repo_id, merge_request.merge_request_id, note_message, discussion.id, mark_as_resolved=True
                )
