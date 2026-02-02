import logging

from django.conf import settings as django_settings

from langgraph.checkpoint.postgres import PostgresSaver

from codebase.base import Scope
from core.utils import generate_uuid
from slash_commands.base import SlashCommand
from slash_commands.decorator import slash_command

logger = logging.getLogger("daiv.slash_commands")


@slash_command(command="clear", scopes=[Scope.ISSUE, Scope.MERGE_REQUEST])
class ClearSlashCommand(SlashCommand):
    """
    Command to clear the conversation context by deleting the thread.
    """

    description: str = "Clear the conversation context and start fresh."

    async def execute_for_agent(
        self, *, args: str, issue_iid: int | None = None, merge_request_id: int | None = None, **kwargs
    ) -> str:
        """
        Execute clear command for agent middleware.

        Deletes the thread associated with the current issue or merge request.

        Args:
            args: Additional parameters from the command (unused).
            issue_iid: The issue IID (for Issue scope).
            merge_request_id: The merge request ID (for Merge Request scope).

        Returns:
            Success or error message.
        """
        if self.scope == Scope.ISSUE and issue_iid is not None:
            thread_id = generate_uuid(f"{self.repo_id}:{self.scope.value}/{issue_iid}")
        elif self.scope == Scope.MERGE_REQUEST and merge_request_id is not None:
            thread_id = generate_uuid(f"{self.repo_id}:{self.scope.value}/{merge_request_id}")
        else:
            return f"The /{self.command} command is only available for issues and merge requests."

        try:
            with PostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
                checkpointer.delete_thread(thread_id)
            logger.info("Thread %s deleted successfully via /%s command", thread_id, self.command)
            return "✅ Conversation context cleared successfully. You can start a fresh conversation now."
        except Exception as e:
            logger.exception("Failed to delete thread %s via /%s command", thread_id, self.command)
            return f"❌ Failed to clear conversation context: {e}"
