from automation.quick_actions.base import QuickAction
from automation.quick_actions.decorator import quick_action


@quick_action
class HelloWorldAction(QuickAction):
    """
    A simple hello world quick action for demonstration purposes.
    """

    identifier = "hello"
    supports_issues = True
    supports_merge_requests = True

    @property
    def description(self) -> str:
        """Get the description of the hello world action."""
        return "Greets the user with a friendly hello message"

    def execute(
        self,
        repo_id: str,
        note: dict,
        user: dict,
        issue: dict | None = None,
        merge_request: dict | None = None,
        params: str | None = None,
    ) -> str:
        """
        Execute the hello world action.

        Args:
            repo_id: The repository ID.
            note: The note data that triggered the action.
            user: The user who triggered the action.
            issue: The issue data (if applicable).
            merge_request: The merge request data (if applicable).
            params: Additional parameters from the command.

        Returns:
            A greeting message mentioning the user's name.
        """
        try:
            username = user.get("username", "there")
            name = user.get("name", username)

            if params:
                return f"Hello {name}! You said: {params}"
            else:
                return f"Hello {name}! 👋 Thanks for using DAIV quick actions!"

        except Exception as e:
            return f"Hello! I encountered an error: {str(e)}"