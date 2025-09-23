from codebase.base import Discussion, Issue, MergeRequest, Note
from codebase.managers.issue_addressor import IssueAddressorManager
from quick_actions.base import BaseAction, QuickAction, Scope, TriggerLocation
from quick_actions.decorator import quick_action


class PlanExecuteAction(BaseAction):
    trigger: str = "execute"
    description: str = "Run or launch the current plan."
    location: TriggerLocation = TriggerLocation.DISCUSSION


class PlanReviseAction(BaseAction):
    trigger: str = "revise"
    description: str = "Discard current plan and create a new one from scratch."
    location: TriggerLocation = TriggerLocation.DISCUSSION


@quick_action(verb="plan", scopes=[Scope.ISSUE])
class PlanQuickAction(QuickAction):
    """
    Actions related to the plan of an issue.
    """

    actions = [PlanExecuteAction, PlanReviseAction]

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
    ) -> None:
        """
        Execute the plan approval action.

        Args:
            repo_id: The repository ID.
            scope: The scope of the quick action.
            discussion: The discussion that triggered the action.
            note: The note that triggered the action.
            issue: The issue where the action was triggered (if applicable).
            merge_request: The merge request where the action was triggered (if applicable).
            args: Additional parameters from the command.
        """
        if PlanExecuteAction.match(args or "", discussion.is_reply):
            await IssueAddressorManager.approve_plan(repo_id, issue.iid)
        elif PlanReviseAction.match(args or "", discussion.is_reply):
            await IssueAddressorManager.plan_issue(repo_id, issue.iid, should_reset_plan=True)
