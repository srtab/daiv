from unittest.mock import MagicMock, patch

from automation.quick_actions.base import QuickAction, Scope
from automation.quick_actions.decorator import quick_action
from codebase.base import Discussion, Issue, MergeRequest, Note


class TestQuickActionDecorator:
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
            pass

    def test_decorator_with_valid_action(self):
        """Test that decorator properly registers a valid quick action."""
        with patch("automation.quick_actions.decorator.quick_action_registry") as mock_registry:
            quick_action(verb="test_action", scopes=[Scope.ISSUE])(self.TestAction)

            # Verify the decorator called register with correct parameters
            mock_registry.register.assert_called_once_with(self.TestAction, "test_action", [Scope.ISSUE])

            # Verify the class is returned unchanged
            assert self.TestAction.__name__ == "TestAction"

    def test_decorator_with_multiple_scopes(self):
        """Test that decorator works with multiple scopes."""
        with patch("automation.quick_actions.decorator.quick_action_registry") as mock_registry:
            scopes = [Scope.ISSUE, Scope.MERGE_REQUEST]

            quick_action(verb="multi_scope_action", scopes=scopes)(self.TestAction)

            mock_registry.register.assert_called_once_with(self.TestAction, "multi_scope_action", scopes)

    def test_decorator_can_be_applied_to_multiple_classes(self):
        """Test that decorator can be applied to multiple different classes."""
        with patch("automation.quick_actions.decorator.quick_action_registry") as mock_registry:
            quick_action(verb="action1", scopes=[Scope.ISSUE])(self.TestAction)

            @quick_action(verb="action2", scopes=[Scope.MERGE_REQUEST])
            class Action2(self.TestAction):
                pass

            # Verify both registrations occurred
            assert mock_registry.register.call_count == 2

            # Check the specific calls
            calls = mock_registry.register.call_args_list
            assert calls[0][0] == (self.TestAction, "action1", [Scope.ISSUE])
            assert calls[1][0] == (Action2, "action2", [Scope.MERGE_REQUEST])

    def test_decorator_with_inheritance(self):
        """Test that decorator works with class inheritance."""
        with patch("automation.quick_actions.decorator.quick_action_registry") as mock_registry:

            class BaseAction(self.TestAction):
                def shared_method(self):
                    return "shared"

            @quick_action(verb="inherited_action", scopes=[Scope.ISSUE])
            class InheritedAction(BaseAction):
                pass

            mock_registry.register.assert_called_once_with(InheritedAction, "inherited_action", [Scope.ISSUE])

            # Verify inheritance still works
            action = InheritedAction()
            assert action.shared_method() == "shared"
