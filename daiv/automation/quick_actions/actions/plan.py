import textwrap
from enum import StrEnum

from automation.quick_actions.base import QuickAction, Scope
from automation.quick_actions.decorator import quick_action
from codebase.api.models import Issue, MergeRequest, Note, User
from codebase.clients import RepoClient
from codebase.managers.issue_addressor import IssueAddressorManager

QUICK_ACTION_VERB = "plan"


class Action(StrEnum):
    EXECUTE = "Run or launch the current plan."
    REVISE = "Discard current plan and create a new one from scratch.."


@quick_action(verb=QUICK_ACTION_VERB, scopes=[Scope.ISSUE])
class PlanAction(QuickAction):
    """
    Actions related to the plan of an issue.
    """

    @staticmethod
    def description() -> str:
        """
        Get the description of the plan action.
        """
        actions = [f"`{action.name.lower()}`" for action in Action]
        return f"Actions related to the plan of an issue. Available actions: {', '.join(actions)}"

    @classmethod
    def help(cls, username: str) -> str:
        """
        Get the help message for the plan action.
        """
        return "\n".join([f" * `@{username} {cls.verb} {action.name.lower()}` - {action.value}" for action in Action])

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
        Execute the plan approval action.

        Args:
            repo_id: The repository ID.
            scope: The scope of the quick action.
            note: The note data that triggered the action.
            user: The user who triggered the action.
            issue: The issue data.
            merge_request: The merge request data (if applicable).
            args: Additional parameters from the command.
        """
        if not args or args[0].lower() not in [action.name.lower() for action in Action]:
            client = RepoClient.create_instance()
            client.create_issue_discussion_note(
                repo_id,
                issue.iid,
                self._invalid_action_message(client.current_user.username, args and args[0] or None),
                note.discussion_id,
            )
            return

        if Action.EXECUTE.name.lower() == args[0].lower():
            await IssueAddressorManager.approve_plan(repo_id, issue.iid, discussion_id=note.discussion_id)
        elif Action.REVISE.name.lower() == args[0].lower():
            await IssueAddressorManager.plan_issue(
                repo_id, issue.iid, should_reset_plan=True, discussion_id=note.discussion_id
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
