from contextlib import suppress
from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from codebase.base import Scope
from slash_commands.base import SlashCommand
from slash_commands.decorator import slash_command

if TYPE_CHECKING:
    from codebase.base import Discussion, Issue


@slash_command(command="clone-to-topic", scopes=[Scope.ISSUE])
class CloneToTopicSlashCommand(SlashCommand):
    """
    Command to clone an issue to all repositories matching specified topics.
    """

    description: str = "Clone this issue to all repositories matching the specified topics."

    def validate_arguments(self, args: str) -> bool:
        """
        Validate that topics are provided.

        Args:
            args: The arguments to validate.

        Returns:
            True if topics are provided, False otherwise.
        """
        return bool(args.strip())

    async def execute_action_for_issue(self, repo_id: str, *, args: str, comment: Discussion, issue: Issue) -> None:
        """
        Clone the issue to all repositories matching the specified topics.

        Args:
            repo_id: The repository ID.
            comment: The comment that triggered the command.
            issue: The issue where the command was triggered.
            args: Comma-separated list of topics.
        """
        # Parse topics from args
        topics = [topic.strip() for topic in args.split(",") if topic.strip()]

        if not topics:
            self._add_invalid_args_message(repo_id, issue.iid, comment.id, args, scope=Scope.ISSUE)
            return

        target_repos = [repo for repo in self.client.list_repositories(topics=topics) if repo.slug != repo_id]

        if not target_repos:
            topics_str = ", ".join([f"`{topic}`" for topic in topics])
            message = f"No repositories matching the specified topics {topics_str} found."
            self.client.create_issue_comment(repo_id, issue.iid, message, reply_to_id=comment.id)
            return

        cloned_issues = []

        for target_repo in target_repos:
            with suppress(Exception):
                cloned_issue_iid = self.client.create_issue(
                    repo_id=target_repo.slug, title=issue.title, description=issue.description, labels=issue.labels
                )

                cloned_issues.append(f"{target_repo.slug}#{cloned_issue_iid}")

        note_message = render_to_string(
            "slash_commands/clone_to_topic_result.txt",
            {"total_count": len(cloned_issues), "cloned_issues": cloned_issues},
        )
        self.client.create_issue_comment(repo_id, issue.iid, note_message, reply_to_id=comment.id)

    async def execute_for_agent(
        self,
        *,
        args: str,
        scope: Scope,
        repo_id: str,
        bot_username: str,
        issue_iid: int | None = None,
        merge_request_id: int | None = None,
    ) -> str:
        """
        Execute clone-to-topic command for agent middleware.

        Returns a message listing the cloned issues or an error message.
        """
        if not self.validate_arguments(args):
            return f"Invalid arguments for /{self.command}. Please provide comma-separated topics."

        if scope != Scope.ISSUE or issue_iid is None:
            return f"The /{self.command} command is only available for issues."

        # Parse topics from args
        topics = [topic.strip() for topic in args.split(",") if topic.strip()]

        if not topics:
            return f"Invalid arguments for /{self.command}. Please provide comma-separated topics."

        # Get the issue details
        issue = self.client.get_issue(repo_id, issue_iid)

        target_repos = [repo for repo in self.client.list_repositories(topics=topics) if repo.slug != repo_id]

        if not target_repos:
            topics_str = ", ".join([f"`{topic}`" for topic in topics])
            return f"No repositories matching the specified topics {topics_str} found."

        cloned_issues = []

        for target_repo in target_repos:
            with suppress(Exception):
                cloned_issue_iid = self.client.create_issue(
                    repo_id=target_repo.slug, title=issue.title, description=issue.description, labels=issue.labels
                )

                cloned_issues.append(f"{target_repo.slug}#{cloned_issue_iid}")

        return render_to_string(
            "slash_commands/clone_to_topic_result.txt",
            {"total_count": len(cloned_issues), "cloned_issues": cloned_issues},
        )
