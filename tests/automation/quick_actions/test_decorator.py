from unittest.mock import patch

from automation.quick_actions.base import QuickAction, Scope
from automation.quick_actions.decorator import quick_action


class TestQuickActionDecorator:
    def test_decorator_with_valid_action(self):
        """Test that decorator properly registers a valid quick action."""
        with patch("automation.quick_actions.decorator.quick_action_registry") as mock_registry:

            @quick_action(verb="test_action", scopes=[Scope.ISSUE])
            class TestAction(QuickAction):
                @property
                def description(self):
                    return "Test action"

                def execute(self, repo_id, scope, note, user, issue=None, merge_request=None, args=None):
                    pass

            # Verify the decorator called register with correct parameters
            mock_registry.register.assert_called_once_with(TestAction, "test_action", [Scope.ISSUE])

            # Verify the class is returned unchanged
            assert TestAction.__name__ == "TestAction"

    def test_decorator_with_multiple_scopes(self):
        """Test that decorator works with multiple scopes."""
        with patch("automation.quick_actions.decorator.quick_action_registry") as mock_registry:
            scopes = [Scope.ISSUE, Scope.MERGE_REQUEST]

            @quick_action(verb="multi_scope_action", scopes=scopes)
            class MultiScopeAction(QuickAction):
                @property
                def description(self):
                    return "Multi scope action"

                def execute(self, repo_id, scope, note, user, issue=None, merge_request=None, args=None):
                    pass

            mock_registry.register.assert_called_once_with(MultiScopeAction, "multi_scope_action", scopes)

    def test_decorator_returns_original_class(self):
        """Test that decorator returns the original class unchanged."""
        with patch("automation.quick_actions.decorator.quick_action_registry"):

            @quick_action(verb="test_action", scopes=[Scope.ISSUE])
            class TestAction(QuickAction):
                @property
                def description(self):
                    return "Test action"

                def execute(self, repo_id, scope, note, user, issue=None, merge_request=None, args=None):
                    return "executed"

            # Verify class functionality is preserved
            action = TestAction()
            assert action.description == "Test action"
            assert action.execute("repo", Scope.ISSUE, {}, {}) == "executed"

    def test_decorator_can_be_applied_to_multiple_classes(self):
        """Test that decorator can be applied to multiple different classes."""
        with patch("automation.quick_actions.decorator.quick_action_registry") as mock_registry:

            @quick_action(verb="action1", scopes=[Scope.ISSUE])
            class Action1(QuickAction):
                @property
                def description(self):
                    return "Action 1"

                def execute(self, repo_id, scope, note, user, issue=None, merge_request=None, args=None):
                    pass

            @quick_action(verb="action2", scopes=[Scope.MERGE_REQUEST])
            class Action2(QuickAction):
                @property
                def description(self):
                    return "Action 2"

                def execute(self, repo_id, scope, note, user, issue=None, merge_request=None, args=None):
                    pass

            # Verify both registrations occurred
            assert mock_registry.register.call_count == 2

            # Check the specific calls
            calls = mock_registry.register.call_args_list
            assert calls[0][0] == (Action1, "action1", [Scope.ISSUE])
            assert calls[1][0] == (Action2, "action2", [Scope.MERGE_REQUEST])

    def test_decorator_with_inheritance(self):
        """Test that decorator works with class inheritance."""
        with patch("automation.quick_actions.decorator.quick_action_registry") as mock_registry:

            class BaseAction(QuickAction):
                def shared_method(self):
                    return "shared"

            @quick_action(verb="inherited_action", scopes=[Scope.ISSUE])
            class InheritedAction(BaseAction):
                @property
                def description(self):
                    return "Inherited action"

                def execute(self, repo_id, scope, note, user, issue=None, merge_request=None, args=None):
                    pass

            mock_registry.register.assert_called_once_with(InheritedAction, "inherited_action", [Scope.ISSUE])

            # Verify inheritance still works
            action = InheritedAction()
            assert action.shared_method() == "shared"
