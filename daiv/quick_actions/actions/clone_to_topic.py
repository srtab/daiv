from contextlib import suppress
from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from quick_actions.base import QuickAction, Scope
from quick_actions.decorator import quick_action

if TYPE_CHECKING:
    from codebase.base import Discussion, Issue


@quick_action(command="clone-to-topic", scopes=[Scope.ISSUE])
class CloneToTopicQuickAction(QuickAction):
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
            comment: The comment that triggered the action.
            issue: The issue where the action was triggered.
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
            "quick_actions/clone_to_topic_result.txt",
            {"total_count": len(cloned_issues), "cloned_issues": cloned_issues},
        )
        self.client.create_issue_comment(repo_id, issue.iid, note_message, reply_to_id=comment.id)
