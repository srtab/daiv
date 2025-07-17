import pytest

from automation.quick_actions.base import QuickAction, Scope


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

    def test_description_is_abstract_property(self):
        """Test that description property is abstract."""

        # Create a concrete subclass without implementing description
        class IncompleteAction(QuickAction):
            def execute(self, repo_id, scope, note, user, issue=None, merge_request=None, args=None):
                pass

        with pytest.raises(TypeError):
            IncompleteAction()

    def test_execute_is_abstract_method(self):
        """Test that execute method is abstract."""

        # Create a concrete subclass without implementing execute
        class IncompleteAction(QuickAction):
            @property
            def description(self):
                return "Test description"

        with pytest.raises(TypeError):
            IncompleteAction()

    def test_concrete_implementation_works(self):
        """Test that a proper concrete implementation can be instantiated."""

        class ConcreteAction(QuickAction):
            @property
            def description(self):
                return "Test description"

            def execute(self, repo_id, scope, note, user, issue=None, merge_request=None, args=None):
                return "executed"

        action = ConcreteAction()
        assert action.description == "Test description"
        result = action.execute("repo1", Scope.ISSUE, {}, {})
        assert result == "executed"

    def test_execute_method_signature(self):
        """Test that execute method has correct signature."""

        class TestAction(QuickAction):
            @property
            def description(self):
                return "Test"

            def execute(self, repo_id, scope, note, user, issue=None, merge_request=None, args=None):
                # Store parameters for verification
                self.last_call = {
                    "repo_id": repo_id,
                    "scope": scope,
                    "note": note,
                    "user": user,
                    "issue": issue,
                    "merge_request": merge_request,
                    "args": args,
                }

        action = TestAction()
        mock_note = {"id": 1}
        mock_user = {"id": 2}
        mock_issue = {"id": 3}
        mock_mr = {"id": 4}

        action.execute(
            repo_id="test_repo",
            scope=Scope.ISSUE,
            note=mock_note,
            user=mock_user,
            issue=mock_issue,
            merge_request=mock_mr,
            args=["arg1", "arg2"],
        )

        assert action.last_call["repo_id"] == "test_repo"
        assert action.last_call["scope"] == Scope.ISSUE
        assert action.last_call["note"] == mock_note
        assert action.last_call["user"] == mock_user
        assert action.last_call["issue"] == mock_issue
        assert action.last_call["merge_request"] == mock_mr
        assert action.last_call["args"] == ["arg1", "arg2"]
