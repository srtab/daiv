import pytest
from quick_actions.base import QuickAction, Scope
from quick_actions.registry import QuickActionRegistry


class MockAction1(QuickAction):
    @property
    def description(self):
        return "Mock action 1"

    def execute(self, repo_id, scope, note, user, issue=None, merge_request=None, args=None):
        pass


class MockAction2(QuickAction):
    @property
    def description(self):
        return "Mock action 2"

    def execute(self, repo_id, scope, note, user, issue=None, merge_request=None, args=None):
        pass


class NotAnAction:
    """Class that doesn't inherit from QuickAction."""

    pass


class TestQuickActionRegistry:
    def test_registry_initialization(self):
        """Test that registry initializes with empty state."""
        registry = QuickActionRegistry()
        assert registry._registry == {}
        assert registry._registry_by_scope == {}

    def test_register_valid_action(self):
        """Test registering a valid quick action."""
        registry = QuickActionRegistry()
        scopes = [Scope.ISSUE, Scope.MERGE_REQUEST]

        registry.register(MockAction1, "test_command", scopes)

        assert "test_command" in registry._registry
        assert registry._registry["test_command"] == MockAction1
        assert getattr(MockAction1, "command", None) == "test_command"
        assert getattr(MockAction1, "scopes", None) == scopes
        assert MockAction1 in registry._registry_by_scope[Scope.ISSUE.value]
        assert MockAction1 in registry._registry_by_scope[Scope.MERGE_REQUEST.value]

    def test_register_invalid_class_raises_assertion(self):
        """Test that registering non-QuickAction class raises AssertionError."""
        registry = QuickActionRegistry()

        with pytest.raises(AssertionError, match="must be a class that inherits from QuickAction"):
            registry.register(NotAnAction, "invalid", [Scope.ISSUE])  # type: ignore

    def test_register_non_class_raises_assertion(self):
        """Test that registering non-class raises AssertionError."""
        registry = QuickActionRegistry()

        with pytest.raises(AssertionError, match="must be a class that inherits from QuickAction"):
            registry.register("not_a_class", "invalid", [Scope.ISSUE])  # type: ignore

    def test_register_duplicate_action_class_raises_assertion(self):
        """Test that registering same action class twice raises AssertionError."""
        registry = QuickActionRegistry()

        registry.register(MockAction1, "first_command", [Scope.ISSUE])

        with pytest.raises(AssertionError, match="is already registered as quick action"):
            registry.register(MockAction1, "second_command", [Scope.ISSUE])

    def test_register_duplicate_command_raises_assertion(self):
        """Test that registering same command twice raises AssertionError."""
        registry = QuickActionRegistry()

        registry.register(MockAction1, "duplicate_command", [Scope.ISSUE])

        with pytest.raises(AssertionError, match="is already registered as quick action"):
            registry.register(MockAction2, "duplicate_command", [Scope.ISSUE])

    def test_register_multiple_scopes(self):
        """Test registering action with multiple scopes."""
        registry = QuickActionRegistry()
        scopes = [Scope.ISSUE, Scope.MERGE_REQUEST]

        registry.register(MockAction1, "multi_scope", scopes)

        assert MockAction1 in registry._registry_by_scope[Scope.ISSUE.value]
        assert MockAction1 in registry._registry_by_scope[Scope.MERGE_REQUEST.value]

    def test_register_single_scope(self):
        """Test registering action with single scope."""
        registry = QuickActionRegistry()
        scopes = [Scope.ISSUE]

        registry.register(MockAction1, "single_scope", scopes)

        assert MockAction1 in registry._registry_by_scope[Scope.ISSUE.value]
        assert (
            Scope.MERGE_REQUEST.value not in registry._registry_by_scope
            or MockAction1 not in registry._registry_by_scope[Scope.MERGE_REQUEST.value]
        )

    def test_get_actions_no_filters(self):
        """Test getting all actions without filters."""
        registry = QuickActionRegistry()

        registry.register(MockAction1, "action1", [Scope.ISSUE])
        registry.register(MockAction2, "action2", [Scope.MERGE_REQUEST])

        actions = registry.get_actions()

        assert len(actions) == 2
        assert MockAction1 in actions
        assert MockAction2 in actions

    def test_get_actions_by_command_only(self):
        """Test getting actions by command only."""
        registry = QuickActionRegistry()

        registry.register(MockAction1, "action1", [Scope.ISSUE])
        registry.register(MockAction2, "action2", [Scope.MERGE_REQUEST])

        actions = registry.get_actions(command="action1")

        assert len(actions) == 1
        assert actions[0] == MockAction1

    def test_get_actions_by_command_not_found(self):
        """Test getting actions by non-existent command."""
        registry = QuickActionRegistry()

        registry.register(MockAction1, "action1", [Scope.ISSUE])

        actions = registry.get_actions(command="nonexistent")

        assert len(actions) == 0

    def test_get_actions_by_scope_only(self):
        """Test getting actions by scope only."""
        registry = QuickActionRegistry()

        registry.register(MockAction1, "action1", [Scope.ISSUE])
        registry.register(MockAction2, "action2", [Scope.MERGE_REQUEST])

        actions = registry.get_actions(scope=Scope.ISSUE)

        assert len(actions) == 1
        assert actions[0] == MockAction1

    def test_get_actions_by_scope_not_found(self):
        """Test getting actions by scope with no registered actions."""
        registry = QuickActionRegistry()

        actions = registry.get_actions(scope=Scope.ISSUE)

        assert len(actions) == 0

    def test_get_actions_by_command_and_scope(self):
        """Test getting actions by both command and scope."""
        registry = QuickActionRegistry()

        registry.register(MockAction1, "action1", [Scope.ISSUE, Scope.MERGE_REQUEST])
        registry.register(MockAction2, "action2", [Scope.ISSUE])

        actions = registry.get_actions(command="action1", scope=Scope.ISSUE)

        assert len(actions) == 1
        assert actions[0] == MockAction1

    def test_get_actions_by_command_and_scope_not_found(self):
        """Test getting actions by command and scope with no matches."""
        registry = QuickActionRegistry()

        registry.register(MockAction1, "action1", [Scope.ISSUE])

        actions = registry.get_actions(command="action1", scope=Scope.MERGE_REQUEST)

        assert len(actions) == 0

    def test_get_actions_multiple_actions_same_scope(self):
        """Test getting multiple actions for same scope."""
        registry = QuickActionRegistry()

        registry.register(MockAction1, "action1", [Scope.ISSUE])
        registry.register(MockAction2, "action2", [Scope.ISSUE])

        actions = registry.get_actions(scope=Scope.ISSUE)

        assert len(actions) == 2
        assert MockAction1 in actions
        assert MockAction2 in actions

    def test_action_attributes_set_correctly(self):
        """Test that action class attributes are set correctly during registration."""
        registry = QuickActionRegistry()
        original_command = getattr(MockAction1, "command", None)
        original_scopes = getattr(MockAction1, "scopes", None)

        try:
            scopes = [Scope.ISSUE, Scope.MERGE_REQUEST]
            registry.register(MockAction1, "test_attributes", scopes)

            assert hasattr(MockAction1, "command") and MockAction1.command == "test_attributes"  # type: ignore
            assert hasattr(MockAction1, "scopes") and MockAction1.scopes == scopes  # type: ignore
        finally:
            # Clean up - restore original attributes if they existed
            if original_command is not None:
                MockAction1.command = original_command
            elif hasattr(MockAction1, "command"):
                delattr(MockAction1, "command")

            if original_scopes is not None:
                MockAction1.scopes = original_scopes
            elif hasattr(MockAction1, "scopes"):
                delattr(MockAction1, "scopes")
