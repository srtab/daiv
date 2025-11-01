from unittest.mock import AsyncMock, MagicMock, patch

from quick_actions.base import Scope
from quick_actions.tasks import execute_issue_task, execute_merge_request_task

from codebase.base import Discussion, Issue, MergeRequest, Note, NoteableType, User


class TestExecuteQuickActionTask:
    def setup_method(self):
        """Set up test fixtures."""
        self.user = User(id=1, username="testuser", name="Test User")
        self.note = Note(
            id=1, body="@bot /help", author=self.user, noteable_type=NoteableType.ISSUE, system=False, resolvable=True
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

    @patch("quick_actions.tasks.quick_action_registry")
    async def test_execute_action_success_issue(self, mock_registry, mock_repo_client):
        """Test successful execution of quick action on issue."""
        # Setup mock action
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_instance.execute_for_issue = AsyncMock()
        mock_action_class.return_value = mock_action_instance
        mock_action_class.can_reply = True
        mock_registry.get_actions.return_value = [mock_action_class]

        # Setup mock repo client
        mock_repo_client.get_issue_comment.return_value = self.discussion
        mock_repo_client.get_issue.return_value = self.issue

        # Execute task with string action args
        await execute_issue_task(
            repo_id="repo123",
            action_command="help",
            action_args="arg1 arg2",
            comment_id=self.discussion.id,
            issue_id=self.issue.iid,
        )

        # Verify registry was called correctly
        mock_registry.get_actions.assert_called_once_with(command="help", scope=Scope.ISSUE)

        # Verify RepoClient calls
        mock_repo_client.get_issue_comment.assert_called_once_with("repo123", self.issue.iid, self.discussion.id)
        mock_repo_client.get_issue.assert_called_once_with("repo123", self.issue.iid)

        # Verify action was instantiated
        mock_action_class.assert_called_once()

        # Verify the execute method was called with correct arguments
        mock_action_instance.execute_for_issue.assert_called_once_with(
            repo_id="repo123", comment=self.discussion, issue=self.issue, args="arg1 arg2"
        )

    @patch("quick_actions.tasks.quick_action_registry")
    async def test_execute_action_success_merge_request(self, mock_registry, mock_repo_client):
        """Test successful execution of quick action on merge request."""
        # Setup mock action
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_instance.execute_for_merge_request = AsyncMock()
        mock_action_class.return_value = mock_action_instance
        mock_action_class.can_reply = True
        mock_registry.get_actions.return_value = [mock_action_class]

        # Setup mock repo client
        mock_repo_client.get_merge_request_comment.return_value = self.discussion
        mock_repo_client.get_merge_request.return_value = self.merge_request

        # Execute task
        await execute_merge_request_task(
            repo_id="repo123",
            action_command="help",
            action_args="",
            comment_id=self.discussion.id,
            merge_request_id=self.merge_request.merge_request_id,
        )

        # Verify registry was called correctly
        mock_registry.get_actions.assert_called_once_with(command="help", scope=Scope.MERGE_REQUEST)

        # Verify RepoClient calls
        mock_repo_client.get_merge_request_comment.assert_called_once_with(
            "repo123", self.merge_request.merge_request_id, "disc-123"
        )
        mock_repo_client.get_merge_request.assert_called_once_with("repo123", self.merge_request.merge_request_id)

        # Verify action was executed
        mock_action_instance.execute_for_merge_request.assert_called_once_with(
            repo_id="repo123", args="", comment=self.discussion, merge_request=self.merge_request
        )

    @patch("quick_actions.tasks.quick_action_registry")
    async def test_action_not_found(self, mock_registry):
        """Test when quick action is not found in registry."""
        mock_registry.get_actions.return_value = []

        # Execute task
        await execute_issue_task(
            repo_id="repo123",
            action_command="nonexistent",
            action_args="",
            comment_id=self.discussion.id,
            issue_id=self.issue.iid,
        )

        # Verify registry was called
        mock_registry.get_actions.assert_called_once_with(command="nonexistent", scope=Scope.ISSUE)

    @patch("quick_actions.tasks.quick_action_registry")
    async def test_multiple_actions_found(self, mock_registry):
        """Test when multiple actions are found for same command/scope."""
        mock_action_class1 = MagicMock()
        mock_action_class1.command = "duplicate"
        mock_action_class2 = MagicMock()
        mock_action_class2.command = "duplicate"

        mock_registry.get_actions.return_value = [mock_action_class1, mock_action_class2]

        # Execute task
        await execute_issue_task(
            repo_id="repo123",
            action_command="duplicate",
            action_args="",
            comment_id=self.discussion.id,
            issue_id=self.issue.iid,
        )

        # Verify registry was called
        mock_registry.get_actions.assert_called_once_with(command="duplicate", scope=Scope.ISSUE)

    @patch("quick_actions.tasks.quick_action_registry")
    async def test_action_execution_exception_issue(self, mock_registry, mock_repo_client):
        """Test handling of exception during action execution on issue."""
        # Setup mock action that raises exception
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_instance.execute_for_issue = AsyncMock(side_effect=Exception("Action failed"))
        mock_action_class.return_value = mock_action_instance
        mock_action_class.can_reply = True
        mock_registry.get_actions.return_value = [mock_action_class]

        # Setup mock repo client
        mock_repo_client.current_user = self.user
        mock_repo_client.get_issue_comment.return_value = self.discussion
        mock_repo_client.get_issue.return_value = self.issue

        # Execute task
        await execute_issue_task(
            repo_id="repo123",
            action_command="failing_action",
            action_args="",
            comment_id=self.discussion.id,
            issue_id=self.issue.iid,
        )

        # Verify error message is posted to issue
        mock_repo_client.create_issue_comment.assert_called_once()
        call_args = mock_repo_client.create_issue_comment.call_args
        assert call_args[0][0] == "repo123"
        assert call_args[0][1] == self.issue.iid
        assert "failing_action" in call_args[0][2]
        assert "reply_to_id" not in call_args[1]  # to make sure we don't reply to the comment as thread

    @patch("quick_actions.tasks.quick_action_registry")
    async def test_action_execution_exception_merge_request(self, mock_registry, mock_repo_client):
        """Test handling of exception during action execution on merge request."""
        # Setup mock action that raises exception
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_instance.execute_for_merge_request = AsyncMock(side_effect=Exception("Action failed"))
        mock_action_class.return_value = mock_action_instance
        mock_action_class.can_reply = True
        mock_registry.get_actions.return_value = [mock_action_class]

        # Setup mock repo client
        mock_repo_client.current_user = self.user
        mock_repo_client.get_merge_request_comment.return_value = self.discussion
        mock_repo_client.get_merge_request.return_value = self.merge_request

        # Execute task
        await execute_merge_request_task(
            repo_id="repo123",
            action_command="failing_action",
            action_args="",
            comment_id=self.discussion.id,
            merge_request_id=self.merge_request.merge_request_id,
        )

        # Verify error message is posted to merge request
        mock_repo_client.create_merge_request_comment.assert_called_once()
        call_args = mock_repo_client.create_merge_request_comment.call_args
        assert call_args[0][0] == "repo123"
        assert call_args[0][1] == self.merge_request.merge_request_id
        assert "failing_action" in call_args[0][2]
        assert "reply_to_id" not in call_args[1]  # to make sure we don't reply to the comment as thread

    @patch("quick_actions.tasks.quick_action_registry")
    async def test_scope_conversion(self, mock_registry, mock_repo_client):
        """Test that string scope is converted to Scope enum."""
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_instance.execute_for_merge_request = AsyncMock()
        mock_action_class.return_value = mock_action_instance
        mock_action_class.can_reply = True
        mock_registry.get_actions.return_value = [mock_action_class]

        # Setup mock repo client
        mock_repo_client.get_merge_request_comment.return_value = self.discussion
        mock_repo_client.get_merge_request.return_value = self.merge_request

        # Execute task with string scope
        await execute_merge_request_task(
            repo_id="repo123",
            action_command="help",
            action_args="",
            comment_id=self.discussion.id,
            merge_request_id=self.merge_request.merge_request_id,
        )

        # Verify scope was converted to enum in both registry call and action execution
        mock_registry.get_actions.assert_called_once_with(command="help", scope=Scope.MERGE_REQUEST)

        mock_action_instance.execute_for_merge_request.assert_called_once()

    @patch("quick_actions.tasks.quick_action_registry")
    async def test_execute_with_empty_action_args(self, mock_registry, mock_repo_client):
        """Test execution with empty action_args string."""
        mock_action_class = MagicMock()
        mock_action_instance = MagicMock()
        mock_action_instance.execute_for_issue = AsyncMock()
        mock_action_class.return_value = mock_action_instance
        mock_action_class.can_reply = True
        mock_registry.get_actions.return_value = [mock_action_class]

        # Setup mock repo client
        mock_repo_client.get_issue_comment.return_value = self.discussion
        mock_repo_client.get_issue.return_value = self.issue

        # Execute task with empty action args
        await execute_issue_task(
            repo_id="repo123",
            action_command="help",
            action_args="",
            comment_id=self.discussion.id,
            issue_id=self.issue.iid,
        )

        # Verify action was executed with empty string
        mock_action_instance.execute_for_issue.assert_called_once_with(
            repo_id="repo123", comment=self.discussion, issue=self.issue, args=""
        )
