from unittest.mock import MagicMock, patch

from automation.quick_actions.base import Scope
from automation.quick_actions.tasks import execute_quick_action_task
from codebase.api.models import Issue, MergeRequest, Note, NoteableType, NoteAction, User


class TestExecuteQuickActionTask:
    def setup_method(self):
        """Set up test fixtures."""
        # Create mock objects since we're testing the task, not the models
        self.note = Note(
            id=1,
            discussion_id="disc-123",
            note="@bot help",
            action=NoteAction.CREATE,
            noteable_type=NoteableType.ISSUE,
            noteable_id=1,
            system=False,
            type="DiscussionNote",
            position=None,
        )

        self.user = User(id=1, username="testuser", name="Test User", email="testuser@example.com")

        self.issue = Issue(
            id=1,
            iid=100,
            title="Test Issue",
            description="Test Issue Description",
            state="open",
            assignee_id=1,
            labels=[],
            type="Issue",
        )

        self.merge_request = MergeRequest(
            id=1,
            iid=200,
            title="Test MR",
            description="Test MR Description",
            state="open",
            source_branch="source_branch",
            target_branch="target_branch",
        )

    @patch("automation.quick_actions.tasks.quick_action_registry")
    def test_execute_action_success_issue(self, mock_registry):
        """Test successful execution of quick action on issue."""
        # Setup mock action
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_class.return_value = mock_action_instance
        mock_registry.get_actions.return_value = [mock_action_class]

        # Execute task
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="help",
            action_scope="issue",
            note=self.note,
            user=self.user,
            issue=self.issue,
            action_args="arg1 arg2",
        )

        # Verify registry was called correctly
        mock_registry.get_actions.assert_called_once_with(verb="help", scope=Scope.ISSUE)

        # Verify action was instantiated and executed
        mock_action_class.assert_called_once()
        mock_action_instance.execute.assert_called_once_with(
            repo_id="repo123",
            scope=Scope.ISSUE,
            note=self.note,
            user=self.user,
            issue=self.issue,
            merge_request=None,
            args="arg1 arg2",
        )

    @patch("automation.quick_actions.tasks.quick_action_registry")
    def test_execute_action_success_merge_request(self, mock_registry):
        """Test successful execution of quick action on merge request."""
        # Setup mock action
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_class.return_value = mock_action_instance
        mock_registry.get_actions.return_value = [mock_action_class]

        # Execute task
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="help",
            action_scope=Scope.MERGE_REQUEST,
            note=self.note,
            user=self.user,
            merge_request=self.merge_request,
        )

        # Verify registry was called correctly
        mock_registry.get_actions.assert_called_once_with(verb="help", scope=Scope.MERGE_REQUEST)

        # Verify action was executed
        mock_action_instance.execute.assert_called_once_with(
            repo_id="repo123",
            scope=Scope.MERGE_REQUEST,
            note=self.note,
            user=self.user,
            issue=None,
            merge_request=self.merge_request,
            args=None,
        )

    @patch("automation.quick_actions.tasks.quick_action_registry")
    def test_action_not_found(self, mock_registry):
        """Test when quick action is not found in registry."""
        mock_registry.get_actions.return_value = []

        # Execute task
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="nonexistent",
            action_scope="issue",
            note=self.note,
            user=self.user,
            issue=self.issue,
        )

    @patch("automation.quick_actions.tasks.quick_action_registry")
    def test_multiple_actions_found(self, mock_registry):
        """Test when multiple actions are found for same verb/scope."""
        mock_action_class1 = MagicMock()
        mock_action_class1.verb = "duplicate"
        mock_action_class2 = MagicMock()
        mock_action_class2.verb = "duplicate"

        mock_registry.get_actions.return_value = [mock_action_class1, mock_action_class2]

        # Execute task
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="duplicate",
            action_scope="issue",
            note=self.note,
            user=self.user,
            issue=self.issue,
        )

    @patch("automation.quick_actions.tasks.RepoClient")
    @patch("automation.quick_actions.tasks.quick_action_registry")
    def test_action_execution_exception_issue(self, mock_registry, mock_repo_client_class):
        """Test handling of exception during action execution on issue."""
        # Setup mock action that raises exception
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_instance.execute.side_effect = Exception("Action failed")
        mock_action_class.return_value = mock_action_instance
        mock_registry.get_actions.return_value = [mock_action_class]

        # Setup mock repo client
        mock_client = MagicMock()
        mock_repo_client_class.create_instance.return_value = mock_client

        # Execute task
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="failing_action",
            action_scope="issue",
            note=self.note,
            user=self.user,
            issue=self.issue,
        )

        # Verify error message is posted to issue
        mock_client.create_issue_discussion_note.assert_called_once_with(
            "repo123", self.issue.iid, "❌ Failed to execute quick action `failing_action`.", self.note.discussion_id
        )

    @patch("automation.quick_actions.tasks.RepoClient")
    @patch("automation.quick_actions.tasks.quick_action_registry")
    def test_action_execution_exception_merge_request(self, mock_registry, mock_repo_client_class):
        """Test handling of exception during action execution on merge request."""
        # Setup mock action that raises exception
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_instance.execute.side_effect = Exception("Action failed")
        mock_action_class.return_value = mock_action_instance
        mock_registry.get_actions.return_value = [mock_action_class]

        # Setup mock repo client
        mock_client = MagicMock()
        mock_repo_client_class.create_instance.return_value = mock_client

        # Execute task
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="failing_action",
            action_scope="merge_request",
            note=self.note,
            user=self.user,
            merge_request=self.merge_request,
        )

        # Verify error message is posted to merge request
        mock_client.create_merge_request_discussion_note.assert_called_once_with(
            "repo123",
            self.merge_request.iid,
            "❌ Failed to execute quick action `failing_action`.",
            self.note.discussion_id,
        )

    @patch("automation.quick_actions.tasks.quick_action_registry")
    def test_execute_with_both_issue_and_merge_request_none(self, mock_registry):
        """Test execution when both issue and merge_request are None."""
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_class.return_value = mock_action_instance
        mock_registry.get_actions.return_value = [mock_action_class]

        # Execute task with both None
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="help",
            action_scope="issue",
            note=self.note,
            user=self.user,
            issue=None,
            merge_request=None,
        )

        # Verify action was executed
        mock_action_instance.execute.assert_called_once_with(
            repo_id="repo123",
            scope=Scope.ISSUE,
            note=self.note,
            user=self.user,
            issue=None,
            merge_request=None,
            args=None,
        )

    @patch("automation.quick_actions.tasks.quick_action_registry")
    def test_scope_conversion(self, mock_registry):
        """Test that string scope is converted to Scope enum."""
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_class.return_value = mock_action_instance
        mock_registry.get_actions.return_value = [mock_action_class]

        # Execute task with string scope
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="help",
            action_scope="merge_request",  # String
            note=self.note,
            user=self.user,
        )

        # Verify scope was converted to enum in both registry call and action execution
        mock_registry.get_actions.assert_called_once_with(
            verb="help",
            scope=Scope.MERGE_REQUEST,  # Should be converted to enum
        )

        mock_action_instance.execute.assert_called_once()
        execute_call_args = mock_action_instance.execute.call_args[1]
        assert execute_call_args["scope"] == Scope.MERGE_REQUEST
