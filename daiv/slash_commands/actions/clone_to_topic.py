from contextlib import suppress

from django.template.loader import render_to_string

from codebase.base import Scope
from codebase.clients import RepoClient
from slash_commands.base import SlashCommand
from slash_commands.decorator import slash_command


@slash_command(command="clone-to-topics", scopes=[Scope.ISSUE])
class CloneToTopicsSlashCommand(SlashCommand):
    """
    Command to clone an issue to all repositories matching specified topics.
    """

    description: str = "Clone this issue to all repositories matching the specified topics."

    async def execute_for_agent(self, *, args: str, issue_iid: int | None = None, **kwargs) -> str:
        """
        Execute clone-to-topic command for agent middleware.

        Returns a message listing the cloned issues or an error message.
        """
        if self.scope != Scope.ISSUE or issue_iid is None:
            return f"The /{self.command} command is only available for issues."

        if not self.validate_arguments(args):
            return f"Invalid arguments for /{self.command}. Please provide comma-separated topics."

        topics = [topic.strip() for topic in args.split(",") if topic.strip()]

        if not topics:
            return f"Invalid arguments for /{self.command}. Please provide comma-separated topics."

        client = RepoClient.create_instance()
        target_repos = [repo for repo in client.list_repositories(topics=topics) if repo.slug != self.repo_id]

        if not target_repos:
            topics_str = ", ".join([f"`{topic}`" for topic in topics])
            return f"No repositories matching the specified topics {topics_str} found."

        issue = client.get_issue(self.repo_id, issue_iid)
        cloned_issues = []

        for target_repo in target_repos:
            with suppress(Exception):
                cloned_issue_iid = client.create_issue(
                    repo_id=target_repo.slug, title=issue.title, description=issue.description, labels=issue.labels
                )

                cloned_issues.append(f"{target_repo.slug}#{cloned_issue_iid}")

        return render_to_string(
            "slash_commands/clone_to_topic_result.txt",
            {"total_count": len(cloned_issues), "cloned_issues": cloned_issues},
        )

    @property
    def command_to_invoke(self) -> str:
        """
        Get the command to activate the action.
        """
        return super().command_to_invoke + " <topic_a>,<topic_b>,..."

    def validate_arguments(self, args: str) -> bool:
        """
        Validate that topics are provided.

        Args:
            args: The arguments to validate.

        Returns:
            True if topics are provided, False otherwise.
        """
        return bool(args.strip())
