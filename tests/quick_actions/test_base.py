from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from quick_actions.base import QuickAction, Scope

if TYPE_CHECKING:
    from codebase.base import Discussion, Issue, MergeRequest


class TestScope:
    def test_scope_enum_values(self):
        """Test that Scope enum has correct values."""
        assert Scope.ISSUE == "Issue"
        assert Scope.MERGE_REQUEST == "Merge Request"

    def test_scope_enum_string_representation(self):
        """Test string representation of Scope enum."""
        assert str(Scope.ISSUE) == "Issue"
        assert str(Scope.MERGE_REQUEST) == "Merge Request"


class TestQuickAction:
    async def test_concrete_implementation_works_for_issue(self):
        """Test that a proper concrete implementation can be instantiated."""

        class ConcreteAction(QuickAction):
            actions = [MagicMock()]

            async def execute_action_for_issue(self, repo_id: str, *, args: str, comment: Discussion, issue: Issue):
                return "executed"

        action = ConcreteAction()
        result = await action.execute_for_issue(args="", repo_id="repo1", comment=MagicMock(), issue=MagicMock())
        assert result == "executed"

    async def test_concrete_implementation_works_for_merge_request(self):
        """Test that execute method has correct signature."""

        class TestAction(QuickAction):
            actions = [MagicMock()]

            async def execute_action_for_merge_request(
                self, repo_id: str, *, args: str, comment: Discussion, merge_request: MergeRequest
            ):
                return "executed"

        action = TestAction()
        result = await action.execute_for_merge_request(
            repo_id="test_repo", args="arg1 arg2", comment=MagicMock(), merge_request=MagicMock()
        )
        assert result == "executed"
