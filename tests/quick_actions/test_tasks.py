from unittest.mock import MagicMock, patch

import pytest
from quick_actions.base import Scope
from quick_actions.tasks import execute_quick_action_task

from codebase.base import Discussion, Issue, MergeRequest, Note, NoteableType, User


class TestExecuteQuickActionTask:
    def setup_method(self):
        """Set up test fixtures."""
        self.user = User(id=1, username="testuser", name="Test User")
        self.note = Note(
            id=1, body="@bot help", author=self.user, noteable_type=NoteableType.ISSUE, system=False, resolvable=True
        )
        self.discussion = Discussion(id="disc-123", notes=[self.note])
        self.issue = Issue(
            id=1, iid=100, title="Test Issue", description="Test Issue Description", state="open", author=self.user
        )
        self.merge_request = MergeRequest(
            repo_id="repo123",
            merge_request_id=200,
            title="Test MR",
            description="Test MR Description",
            source_branch="source_branch",
            target_branch="target_branch",
        )

    @patch("quick_actions.tasks.async_to_sync")
    @patch("quick_actions.tasks.quick_action_registry")
    def test_execute_action_success_issue(self, mock_registry, mock_async_to_sync, mock_repo_client):
        """Test successful execution of quick action on issue."""
        # Setup mock action
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_class.return_value = mock_action_instance
        mock_action_class.can_reply = True
        mock_registry.get_actions.return_value = [mock_action_class]

        # Mock async_to_sync to return a callable that calls the async method
        mock_sync_execute = MagicMock()
        mock_async_to_sync.return_value = mock_sync_execute

        # Setup mock repo client
        mock_repo_client.get_issue_discussion.return_value = self.discussion
        mock_repo_client.get_issue.return_value = self.issue

        # Execute task with string action args
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="help",
            action_scope=Scope.ISSUE.value,
            action_args="arg1 arg2",
            discussion_id=self.discussion.id,
            note_id=self.note.id,
            issue_id=self.issue.id,
        )

        # Verify registry was called correctly
        mock_registry.get_actions.assert_called_once_with(verb="help", scope=Scope.ISSUE)

        # Verify RepoClient calls
        mock_repo_client.get_issue_discussion.assert_called_once_with(
            "repo123", self.issue.id, self.discussion.id, only_resolvable=False
        )
        mock_repo_client.get_issue.assert_called_once_with("repo123", self.issue.id)

        # Verify action was instantiated
        mock_action_class.assert_called_once()

        # Verify async_to_sync was called with the action's execute method
        mock_async_to_sync.assert_called_once_with(mock_action_instance.execute)

        # Verify the synced execute method was called with correct arguments
        mock_sync_execute.assert_called_once_with(
            repo_id="repo123",
            scope=Scope.ISSUE,
            discussion=self.discussion,
            note=self.note,
            issue=self.issue,
            merge_request=None,
            args="arg1 arg2",
        )

    @patch("quick_actions.tasks.async_to_sync")
    @patch("quick_actions.tasks.quick_action_registry")
    def test_execute_action_success_merge_request(self, mock_registry, mock_async_to_sync, mock_repo_client):
        """Test successful execution of quick action on merge request."""
        # Setup mock action
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_class.return_value = mock_action_instance
        mock_action_class.can_reply = True
        mock_registry.get_actions.return_value = [mock_action_class]

        # Mock async_to_sync to return a callable that calls the async method
        mock_sync_execute = MagicMock()
        mock_async_to_sync.return_value = mock_sync_execute

        # Setup mock repo client
        mock_repo_client.get_merge_request_discussion.return_value = self.discussion
        mock_repo_client.get_merge_request.return_value = self.merge_request

        # Execute task
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="help",
            action_scope=Scope.MERGE_REQUEST.value,
            action_args="",
            discussion_id="disc-123",
            note_id=self.note.id,
            merge_request_id=self.merge_request.merge_request_id,
        )

        # Verify registry was called correctly
        mock_registry.get_actions.assert_called_once_with(verb="help", scope=Scope.MERGE_REQUEST)

        # Verify RepoClient calls
        mock_repo_client.get_merge_request_discussion.assert_called_once_with(
            "repo123", self.merge_request.merge_request_id, "disc-123", only_resolvable=False
        )
        mock_repo_client.get_merge_request.assert_called_once_with("repo123", self.merge_request.merge_request_id)

        # Verify action was executed
        mock_sync_execute.assert_called_once_with(
            repo_id="repo123",
            args="",
            scope=Scope.MERGE_REQUEST,
            discussion=self.discussion,
            note=self.note,
            issue=None,
            merge_request=self.merge_request,
        )

    @patch("quick_actions.tasks.quick_action_registry")
    def test_action_not_found(self, mock_registry):
        """Test when quick action is not found in registry."""
        mock_registry.get_actions.return_value = []

        # Execute task
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="nonexistent",
            action_scope=Scope.ISSUE.value,
            action_args="",
            discussion_id=self.discussion.id,
            note_id=self.note.id,
            issue_id=self.issue.id,
        )

        # Verify registry was called
        mock_registry.get_actions.assert_called_once_with(verb="nonexistent", scope=Scope.ISSUE)

    @patch("quick_actions.tasks.quick_action_registry")
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
            action_scope=Scope.ISSUE.value,
            action_args="",
            discussion_id=self.discussion.id,
            note_id=self.note.id,
            issue_id=self.issue.id,
        )

        # Verify registry was called
        mock_registry.get_actions.assert_called_once_with(verb="duplicate", scope=Scope.ISSUE)

    @patch("quick_actions.tasks.async_to_sync")
    @patch("quick_actions.tasks.quick_action_registry")
    def test_action_execution_exception_issue(self, mock_registry, mock_async_to_sync, mock_repo_client):
        """Test handling of exception during action execution on issue."""
        # Setup mock action that raises exception
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_class.return_value = mock_action_instance
        mock_action_class.can_reply = True
        mock_registry.get_actions.return_value = [mock_action_class]

        # Mock async_to_sync to return a callable that raises exception
        mock_sync_execute = MagicMock()
        mock_sync_execute.side_effect = Exception("Action failed")
        mock_async_to_sync.return_value = mock_sync_execute

        # Setup mock repo client
        mock_repo_client.current_user = self.user
        mock_repo_client.get_issue_discussion.return_value = self.discussion
        mock_repo_client.get_issue.return_value = self.issue

        # Execute task
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="failing_action",
            action_scope=Scope.ISSUE.value,
            action_args="",
            discussion_id=self.discussion.id,
            note_id=self.note.id,
            issue_id=self.issue.id,
        )

        # Verify error message is posted to issue
        mock_repo_client.create_issue_discussion_note.assert_called_once()
        call_args = mock_repo_client.create_issue_discussion_note.call_args
        assert call_args[0][0] == "repo123"
        assert call_args[0][1] == self.issue.iid
        assert "failing_action" in call_args[0][2]
        assert call_args[0][3] == "disc-123"

    @patch("quick_actions.tasks.async_to_sync")
    @patch("quick_actions.tasks.quick_action_registry")
    def test_action_execution_exception_merge_request(self, mock_registry, mock_async_to_sync, mock_repo_client):
        """Test handling of exception during action execution on merge request."""
        # Setup mock action that raises exception
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_class.return_value = mock_action_instance
        mock_action_class.can_reply = True
        mock_registry.get_actions.return_value = [mock_action_class]

        # Mock async_to_sync to return a callable that raises exception
        mock_sync_execute = MagicMock()
        mock_sync_execute.side_effect = Exception("Action failed")
        mock_async_to_sync.return_value = mock_sync_execute

        # Setup mock repo client
        mock_repo_client.current_user = self.user
        mock_repo_client.get_merge_request_discussion.return_value = self.discussion
        mock_repo_client.get_merge_request.return_value = self.merge_request

        # Execute task
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="failing_action",
            action_scope=Scope.MERGE_REQUEST.value,
            action_args="",
            discussion_id="disc-123",
            note_id=self.note.id,
            merge_request_id=self.merge_request.merge_request_id,
        )

        # Verify error message is posted to merge request
        mock_repo_client.create_merge_request_comment.assert_called_once()
        call_args = mock_repo_client.create_merge_request_comment.call_args
        assert call_args[0][0] == "repo123"
        assert call_args[0][1] == self.merge_request.merge_request_id
        assert "failing_action" in call_args[0][2]
        assert call_args[1]["reply_to_id"] == "disc-123"

    @patch("quick_actions.tasks.async_to_sync")
    @patch("quick_actions.tasks.quick_action_registry")
    def test_execute_with_both_issue_and_merge_request_none(self, mock_registry, mock_async_to_sync, mock_repo_client):
        """Test execution when both issue and merge_request are None."""
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_class.return_value = mock_action_instance
        mock_action_class.can_reply = True
        mock_registry.get_actions.return_value = [mock_action_class]

        # Mock async_to_sync to return a callable that calls the async method
        mock_sync_execute = MagicMock()
        mock_async_to_sync.return_value = mock_sync_execute

        # Setup mock repo client
        mock_repo_client.get_issue_discussion.return_value = self.discussion
        mock_repo_client.get_issue.return_value = None

        with pytest.raises(AssertionError, match="Either issue_id or merge_request_id must be provided"):
            execute_quick_action_task(
                repo_id="repo123",
                action_verb="help",
                action_scope=Scope.ISSUE.value,
                action_args="",
                discussion_id=self.discussion.id,
                note_id=self.note.id,
                issue_id=None,
                merge_request_id=None,
            )

    @patch("quick_actions.tasks.async_to_sync")
    @patch("quick_actions.tasks.quick_action_registry")
    def test_scope_conversion(self, mock_registry, mock_async_to_sync, mock_repo_client):
        """Test that string scope is converted to Scope enum."""
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_class.return_value = mock_action_instance
        mock_action_class.can_reply = True
        mock_registry.get_actions.return_value = [mock_action_class]

        # Mock async_to_sync to return a callable that calls the async method
        mock_sync_execute = MagicMock()
        mock_async_to_sync.return_value = mock_sync_execute

        # Setup mock repo client
        mock_repo_client.get_merge_request_discussion.return_value = self.discussion
        mock_repo_client.get_merge_request.return_value = self.merge_request

        # Execute task with string scope
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="help",
            action_scope=Scope.MERGE_REQUEST.value,
            action_args="",
            discussion_id=self.discussion.id,
            note_id=self.note.id,
            merge_request_id=self.merge_request.merge_request_id,
        )

        # Verify scope was converted to enum in both registry call and action execution
        mock_registry.get_actions.assert_called_once_with(
            verb="help",
            scope=Scope.MERGE_REQUEST,  # Should be converted to enum
        )

        mock_sync_execute.assert_called_once()
        execute_call_args = mock_sync_execute.call_args[1]
        assert execute_call_args["scope"] == Scope.MERGE_REQUEST

    @patch("quick_actions.tasks.async_to_sync")
    @patch("quick_actions.tasks.quick_action_registry")
    def test_execute_with_empty_action_args(self, mock_registry, mock_async_to_sync, mock_repo_client):
        """Test execution with empty action_args string."""
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_class.return_value = mock_action_instance
        mock_action_class.can_reply = True
        mock_registry.get_actions.return_value = [mock_action_class]

        # Mock async_to_sync to return a callable that calls the async method
        mock_sync_execute = MagicMock()
        mock_async_to_sync.return_value = mock_sync_execute

        # Setup mock repo client
        mock_repo_client.get_issue_discussion.return_value = self.discussion
        mock_repo_client.get_issue.return_value = self.issue

        # Execute task with empty action args
        execute_quick_action_task(
            repo_id="repo123",
            action_verb="help",
            action_scope=Scope.ISSUE.value,
            action_args="",
            discussion_id=self.discussion.id,
            note_id=self.note.id,
            issue_id=self.issue.id,
        )

        # Verify action was executed with empty string
        mock_sync_execute.assert_called_once_with(
            repo_id="repo123",
            scope=Scope.ISSUE,
            discussion=self.discussion,
            note=self.note,
            issue=self.issue,
            merge_request=None,
            args="",
        )
