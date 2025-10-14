from typing import TYPE_CHECKING

from langchain_core.prompts.string import jinja2_formatter

from core.constants import BOT_NAME
from quick_actions.base import QuickAction, Scope
from quick_actions.decorator import quick_action
from quick_actions.registry import quick_action_registry
from quick_actions.templates import QUICK_ACTIONS_TEMPLATE

if TYPE_CHECKING:
    from codebase.base import Discussion, Issue, MergeRequest


@quick_action(command="help", scopes=[Scope.ISSUE, Scope.MERGE_REQUEST])
class HelpQuickAction(QuickAction):
    """
    Shows the help message for the available quick actions.
    """

    description: str = "Shows the help message with the available quick actions."

    async def execute_action_for_issue(self, repo_id: str, *, args: str, comment: Discussion, issue: Issue) -> None:
        """
        Execute the help action.

        Args:
            repo_id: The repository ID.
            args: Additional parameters from the command.
            comment: The comment that triggered the action.
            issue: The issue where the action was triggered (if applicable).
        """
        if note_message := self._get_note_message(Scope.ISSUE):
            self.client.create_issue_comment(repo_id, issue.iid, note_message)

    async def execute_action_for_merge_request(
        self, repo_id: str, *, args: str, comment: Discussion, merge_request: MergeRequest
    ) -> None:
        """
        Execute the help action.

        Args:
            repo_id: The repository ID.
            args: Additional parameters from the command.
            comment: The comment that triggered the action.
            merge_request: The merge request where the action was triggered (if applicable).
        """
        if note_message := self._get_note_message(Scope.MERGE_REQUEST):
            self.client.create_merge_request_comment(repo_id, merge_request.merge_request_id, note_message)

    def _get_note_message(self, scope: Scope) -> str | None:
        """
        Get the note message for the given scope.
        """
        actions_help = [action().help() for action in quick_action_registry.get_actions(scope=scope)]
        if not actions_help:
            return None
        return jinja2_formatter(QUICK_ACTIONS_TEMPLATE, bot_name=BOT_NAME, scope=scope, actions=actions_help)
