from unittest.mock import MagicMock, patch

from automation.quick_actions.actions.echo import EchoQuickAction
from automation.quick_actions.actions.help import HelpQuickAction
from automation.quick_actions.base import Scope


class TestHelpAction:
    def setup_method(self):
        """Set up test fixtures."""
        self.action = HelpQuickAction()
        self.action.client = MagicMock(current_user=MagicMock(username="bot"))

        # Create mock objects
        self.mock_note = MagicMock()
        self.mock_note.id = 1
        self.mock_note.discussion_id = "disc-123"

        self.mock_discussion = MagicMock()
        self.mock_discussion.id = "disc-123"
        self.mock_discussion.notes = [self.mock_note]

        self.mock_user = MagicMock()
        self.mock_user.id = 1
        self.mock_user.username = "testuser"

        self.mock_issue = MagicMock()
        self.mock_issue.id = 1
        self.mock_issue.iid = 100

        self.mock_merge_request = MagicMock()
        self.mock_merge_request.merge_request_id = 1

    def test_help_action_has_correct_attributes(self):
        """Test that HelpAction has the expected attributes set by decorator."""
        assert hasattr(HelpQuickAction, "verb")
        assert hasattr(HelpQuickAction, "scopes")
        assert HelpQuickAction.verb == "help"
        assert Scope.ISSUE in HelpQuickAction.scopes
        assert Scope.MERGE_REQUEST in HelpQuickAction.scopes

    @patch("automation.quick_actions.actions.help.quick_action_registry")
    async def test_execute_on_issue(self, mock_registry):
        """Test executing help action on an issue."""
        # Setup mock registry with actions
        mock_action1 = MagicMock(verb="help")
        mock_action1.help.return_value = "- `@bot help` - Shows help"

        mock_action2 = MagicMock(verb="status")
        mock_action2.help.return_value = "- `@bot status` - Shows status"

        mock_registry.get_actions.return_value = [mock_action1, mock_action2]

        # Execute the action
        await self.action.execute(
            repo_id="repo123",
            args="",
            scope=Scope.ISSUE,
            discussion=self.mock_discussion,
            note=self.mock_note,
            issue=self.mock_issue,
        )

        # Verify registry was called with correct scope
        mock_registry.get_actions.assert_called_once_with(scope=Scope.ISSUE)

        # Verify issue discussion note was created
        self.action.client.create_issue_discussion_note.assert_called_once()

    @patch("automation.quick_actions.actions.help.quick_action_registry")
    async def test_execute_on_merge_request(self, mock_registry):
        """Test executing help action on a merge request."""
        # Setup mock registry with actions
        mock_action = MagicMock(verb="help")
        mock_action.help.return_value = "- `@bot help` - Shows help"

        mock_registry.get_actions.return_value = [mock_action]

        # Execute the action
        await self.action.execute(
            repo_id="repo123",
            args="",
            scope=Scope.MERGE_REQUEST,
            discussion=self.mock_discussion,
            note=self.mock_note,
            merge_request=self.mock_merge_request,
        )

        # Verify registry was called with correct scope
        mock_registry.get_actions.assert_called_once_with(scope=Scope.MERGE_REQUEST)

        # Verify merge request discussion note was created
        self.action.client.create_merge_request_discussion_note.assert_called_once()

    @patch("automation.quick_actions.actions.help.quick_action_registry")
    async def test_execute_with_multiple_actions(self, mock_registry):
        """Test executing help action with multiple available actions."""
        # Setup mock registry with multiple actions
        mock_actions = []
        action_verbs = ["help", "status", "assign", "close"]
        descriptions = ["Shows help", "Shows status", "Assigns issue", "Closes issue"]

        for verb, desc in zip(action_verbs, descriptions, strict=False):
            mock_action = MagicMock(verb=verb)
            mock_action.help.return_value = f"- `@daivbot {verb}` - {desc}"
            mock_actions.append(mock_action)

        mock_registry.get_actions.return_value = mock_actions

        # Execute the action
        await self.action.execute(
            repo_id="repo123",
            args="",
            scope=Scope.ISSUE,
            discussion=self.mock_discussion,
            note=self.mock_note,
            issue=self.mock_issue,
        )

        # Verify issue discussion note was created
        self.action.client.create_issue_discussion_note.assert_called_once()
        call_args = self.action.client.create_issue_discussion_note.call_args

        # Verify message content contains all actions
        message = call_args[0][2]
        for verb, desc in zip(action_verbs, descriptions, strict=False):
            assert f"@daivbot {verb}" in message
            assert desc in message


class TestEchoAction:
    def setup_method(self):
        """Set up test fixtures."""
        self.action = EchoQuickAction()
        self.action.client = MagicMock()

        # Create mock objects
        self.mock_note = MagicMock()
        self.mock_note.id = 1
        self.mock_note.discussion_id = "disc-123"

        self.mock_discussion = MagicMock()
        self.mock_discussion.id = "disc-123"
        self.mock_discussion.notes = [self.mock_note]

        self.mock_user = MagicMock()
        self.mock_user.id = 1
        self.mock_user.username = "testuser"

        self.mock_issue = MagicMock()
        self.mock_issue.id = 1
        self.mock_issue.iid = 100

        self.mock_merge_request = MagicMock()
        self.mock_merge_request.merge_request_id = 1

    def test_echo_action_has_correct_attributes(self):
        """Test that EchoQuickAction has the expected attributes set by decorator."""
        assert hasattr(EchoQuickAction, "verb")
        assert hasattr(EchoQuickAction, "scopes")
        assert EchoQuickAction.verb == "echo"
        assert Scope.ISSUE in EchoQuickAction.scopes
        assert Scope.MERGE_REQUEST in EchoQuickAction.scopes

    def test_description_property(self):
        """Test that the description method returns expected string."""
        assert self.action.actions[0].description == "Echoes back the provided message."

    async def test_execute_on_issue_with_args(self):
        """Test echoing with arguments on an issue scope."""
        # Execute the action with arguments
        await self.action.execute_action(
            repo_id="repo123",
            args="hello world test",
            scope=Scope.ISSUE,
            discussion=self.mock_discussion,
            note=self.mock_note,
            issue=self.mock_issue,
        )

        # Verify issue discussion note was created with correct message
        self.action.client.create_issue_discussion_note.assert_called_once_with(
            "repo123", 100, "Echo: hello world test", "disc-123"
        )

    async def test_execute_on_issue_without_args(self):
        """Test echoing without arguments on an issue scope."""
        # Execute the action without arguments
        await self.action.execute_action(
            repo_id="repo123",
            args="",
            scope=Scope.ISSUE,
            discussion=self.mock_discussion,
            note=self.mock_note,
            issue=self.mock_issue,
        )

        # Verify issue discussion note was created with "no message" response
        self.action.client.create_issue_discussion_note.assert_called_once_with(
            "repo123", 100, "No message to echo", "disc-123"
        )

    async def test_execute_on_merge_request_with_args(self):
        """Test echoing with arguments on merge request scope."""
        # Execute the action with arguments
        await self.action.execute_action(
            repo_id="repo123",
            args="test message for MR",
            scope=Scope.MERGE_REQUEST,
            discussion=self.mock_discussion,
            note=self.mock_note,
            merge_request=self.mock_merge_request,
        )

        # Verify merge request discussion note was created with correct message
        self.action.client.create_merge_request_discussion_note.assert_called_once_with(
            "repo123", 1, "Echo: test message for MR", "disc-123", mark_as_resolved=True
        )

    async def test_execute_on_merge_request_without_args(self):
        """Test echoing without arguments on merge request scope."""
        # Execute the action without arguments
        await self.action.execute_action(
            repo_id="repo123",
            args="   ",  # Test with whitespace-only args
            scope=Scope.MERGE_REQUEST,
            discussion=self.mock_discussion,
            note=self.mock_note,
            merge_request=self.mock_merge_request,
        )

        # Verify merge request discussion note was created with "no message" response
        self.action.client.create_merge_request_discussion_note.assert_called_once_with(
            "repo123", 1, "No message to echo", "disc-123", mark_as_resolved=True
        )
