import textwrap

from automation.quick_actions.base import BaseAction, QuickAction, Scope
from automation.quick_actions.decorator import quick_action
from codebase.base import Discussion, Issue, MergeRequest, Note
from codebase.clients import RepoClient
from codebase.managers.issue_addressor import IssueAddressorManager

QUICK_ACTION_VERB = "plan"


class Action(BaseAction):
    EXECUTE = "Run or launch the current plan."
    REVISE = "Discard current plan and create a new one from scratch.."


@quick_action(verb=QUICK_ACTION_VERB, scopes=[Scope.ISSUE])
class PlanQuickAction(QuickAction):
    """
    Actions related to the plan of an issue.
    """

    @staticmethod
    def description() -> str:
        """
        Get the description of the plan action.
        """
        return "Actions related to the plan of an issue."

    @classmethod
    def help(cls, username: str) -> str:
        """
        Get the help message for the plan action.
        """
        return "\n".join([
            f" * `@{username} {cls.verb} {Action.get_name(action)}` - {action.value}" for action in Action
        ])

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
        if not args or not self._validate_action(args, discussion):
            client = RepoClient.create_instance()
            client.create_issue_discussion_note(
                repo_id,
                issue.iid,
                self._invalid_action_message(client.current_user.username, args or None),
                discussion.id,
            )
            return

        if Action.get_name(Action.EXECUTE) == args:
            await IssueAddressorManager.approve_plan(repo_id, issue.iid, discussion_id=discussion.id)
        elif Action.get_name(Action.REVISE) == args:
            await IssueAddressorManager.plan_issue(
                repo_id, issue.iid, should_reset_plan=True, discussion_id=discussion.id
            )

    def _validate_action(self, action: str, discussion: Discussion) -> bool:
        """
        Validate the action is valid.
        """
        return action.lower() in [Action.get_name(action) for action in Action] and (
            # Need to be the first note in the discussion to execute the plan
            action == Action.get_name(Action.EXECUTE)
            and len(discussion.notes) == 1
            # Need to be the first note in the discussion to revise the plan
            or action == Action.get_name(Action.REVISE)
            and len(discussion.notes) == 1
        )

    def _invalid_action_message(self, username: str, invalid_action: str | None) -> str:
        """
        Get the help message for the plan action.
        """
        return textwrap.dedent(
            f"""\
            ❌ The action `{invalid_action or "no action"}` is not valid.

            The available actions for the `{QUICK_ACTION_VERB}` are as follows:
            """
        ) + self.help(username)
