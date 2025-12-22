from typing import TYPE_CHECKING

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
        # async with set_runtime_ctx(repo_id, scope="issue") as runtime_ctx:
        #     await IssueAddressorManager.approve_plan(issue_iid=issue.iid, runtime_ctx=runtime_ctx) # noqa: E501 ERA001


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
        # async with set_runtime_ctx(repo_id, scope="issue") as runtime_ctx:
        #     await IssueAddressorManager.plan_issue(issue_iid=issue.iid, runtime_ctx=runtime_ctx, should_reset_plan=True) # noqa: E501 ERA001
