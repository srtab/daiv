from typing import TYPE_CHECKING

from codebase.context import set_repository_ctx
from codebase.managers.issue_addressor import IssueAddressorManager
from quick_actions.base import QuickAction, Scope
from quick_actions.decorator import quick_action

if TYPE_CHECKING:
    from codebase.base import Discussion, Issue


@quick_action(command="approve-plan", scopes=[Scope.ISSUE])
class ApprovePlanQuickAction(QuickAction):
    """
    Command to approve the plan of an issue.
    """

    description: str = "Approve the current plan and execute it."

    async def execute_action_for_issue(self, repo_id: str, *, args: str, comment: Discussion, issue: Issue) -> None:
        """
        Approve the current plan and execute it.

        Args:
            repo_id: The repository ID.
            comment: The comment that triggered the action.
            issue: The issue where the action was triggered (if applicable).
            args: Additional parameters from the command.
        """
        async with set_repository_ctx(repo_id):
            await IssueAddressorManager.approve_plan(repo_id, issue.iid)


@quick_action(command="revise-plan", scopes=[Scope.ISSUE])
class RevisePlanQuickAction(QuickAction):
    """
    Command to revise the plan of an issue.
    """

    description: str = "Discard current plan and create a new one from scratch."

    async def execute_action_for_issue(self, repo_id: str, *, args: str, comment: Discussion, issue: Issue) -> None:
        """
        Discard current plan and create a new one from scratch.

        Args:
            repo_id: The repository ID.
            comment: The comment that triggered the action.
            issue: The issue where the action was triggered (if applicable).
            args: Additional parameters from the command.
        """
        async with set_repository_ctx(repo_id):
            await IssueAddressorManager.plan_issue(repo_id, issue.iid, should_reset_plan=True)
