from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from quick_actions.base import QuickAction, Scope

if TYPE_CHECKING:
    from codebase.base import Discussion, Issue, MergeRequest, Note


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
    def test_quick_action_is_abstract(self):
        """Test that QuickAction cannot be instantiated directly."""
        with pytest.raises(TypeError):
            QuickAction()

    def test_execute_is_abstract_method(self):
        """Test that execute method is abstract."""

        # Create a concrete subclass without implementing execute
        class IncompleteAction(QuickAction):
            pass

        with pytest.raises(TypeError):
            IncompleteAction()

    async def test_concrete_implementation_works(self):
        """Test that a proper concrete implementation can be instantiated."""

        class ConcreteAction(QuickAction):
            actions = [MagicMock()]

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
                is_reply: bool = False,
            ):
                return "executed"

        action = ConcreteAction()
        result = await action.execute(
            args="",
            repo_id="repo1",
            scope=Scope.ISSUE,
            note=MagicMock(),
            discussion=MagicMock(),
            issue=MagicMock(),
            merge_request=MagicMock(),
        )
        assert result == "executed"

    async def test_execute_method_signature(self):
        """Test that execute method has correct signature."""

        class TestAction(QuickAction):
            actions = [MagicMock()]

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
                is_reply: bool = False,
            ) -> None:
                # Store parameters for verification
                self.last_call = {
                    "repo_id": repo_id,
                    "scope": scope,
                    "note": note,
                    "issue": issue,
                    "discussion": discussion,
                    "merge_request": merge_request,
                    "args": args,
                }

        action = TestAction()
        mock_note = MagicMock()
        mock_issue = MagicMock()
        mock_mr = MagicMock()
        mock_discussion = MagicMock()

        await action.execute(
            repo_id="test_repo",
            args="arg1 arg2",
            scope=Scope.ISSUE,
            discussion=mock_discussion,
            note=mock_note,
            issue=mock_issue,
            merge_request=mock_mr,
        )

        assert action.last_call["repo_id"] == "test_repo"
        assert action.last_call["scope"] == Scope.ISSUE
        assert action.last_call["note"] == mock_note
        assert action.last_call["discussion"] == mock_discussion
        assert action.last_call["issue"] == mock_issue
        assert action.last_call["merge_request"] == mock_mr
        assert action.last_call["args"] == "arg1 arg2"
