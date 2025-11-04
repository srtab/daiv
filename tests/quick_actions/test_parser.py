import pytest

from quick_actions.parser import parse_quick_action


class TestParseQuickAction:
    def test_parse_simple_command(self):
        """Test parsing a simple bot command."""
        note_body = "@testbot /help"
        result = parse_quick_action(note_body, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.args == []
        assert result.raw == "@testbot /help"

    def test_parse_command_with_args(self):
        """Test parsing a command with arguments."""
        note_body = "@testbot /assign user1 user2"
        result = parse_quick_action(note_body, "testbot")

        assert result is not None
        assert result.command == "assign"
        assert result.args == ["user1", "user2"]
        assert result.raw == "@testbot /assign user1 user2"

    def test_parse_command_with_quoted_args(self):
        """Test parsing a command with quoted arguments."""
        note_body = '@testbot /create "test issue" --label "bug fix"'
        result = parse_quick_action(note_body, "testbot")

        assert result is not None
        assert result.command == "create"
        assert result.args == ["test issue", "--label", "bug fix"]
        assert result.raw == '@testbot /create "test issue" --label "bug fix"'

    def test_parse_case_insensitive_bot_name(self):
        """Test that bot name matching is case insensitive."""
        note_body = "@TestBot /help"
        result = parse_quick_action(note_body, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.raw == "@TestBot /help"

    def test_parse_case_insensitive_command(self):
        """Test that command is converted to lowercase."""
        note_body = "@testbot /HELP"
        result = parse_quick_action(note_body, "testbot")

        assert result is not None
        assert result.command == "help"  # Should be lowercase

    def test_parse_command_in_middle_of_text(self):
        """Test parsing command that appears in middle of note."""
        note_body = "Some text before\n@testbot /help\nSome text after"
        result = parse_quick_action(note_body, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.raw == "@testbot /help"

    def test_parse_first_command_only(self):
        """Test that only first command is parsed when multiple exist."""
        note_body = "@testbot /help\n@testbot /status"
        result = parse_quick_action(note_body, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.raw == "@testbot /help"

    def test_parse_no_command_found(self):
        """Test when no bot command is found."""
        note_body = "Just some regular text without commands"
        result = parse_quick_action(note_body, "testbot")

        assert result is None

    def test_parse_different_bot_name(self):
        """Test that commands for different bots are ignored."""
        note_body = "@otherbot /help"
        result = parse_quick_action(note_body, "testbot")

        assert result is None

    def test_parse_email_address_ignored(self):
        """Test that email addresses are not matched as bot commands."""
        note_body = "Contact testbot@example.com for help"
        result = parse_quick_action(note_body, "testbot")

        assert result is None

    def test_parse_partial_bot_name_ignored(self):
        """Test that partial bot name matches are ignored."""
        note_body = "@testbotx /help"  # Extra character
        result = parse_quick_action(note_body, "testbot")

        assert result is None

    def test_parse_bot_name_with_special_chars(self):
        """Test parsing bot name that contains special regex characters."""
        note_body = "@test.bot /help"
        result = parse_quick_action(note_body, "test.bot")

        assert result is not None
        assert result.command == "help"

    def test_parse_command_with_newline_in_middle(self):
        """Test that commands stop at newlines."""
        note_body = "@testbot /help arg1\nthis should not be included"
        result = parse_quick_action(note_body, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.args == ["arg1"]
        assert "this should not be included" not in result.raw

    def test_parse_empty_command(self):
        """Test parsing when bot is mentioned but no command follows."""
        note_body = "@testbot   "  # Just whitespace after mention
        result = parse_quick_action(note_body, "testbot")

        assert result is None

    def test_parse_command_with_tabs(self):
        """Test parsing command with tab characters."""
        note_body = "@testbot\t\t/help\targ1"
        result = parse_quick_action(note_body, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.args == ["arg1"]

    def test_parse_command_with_complex_quoting(self):
        """Test parsing command with complex shell-style quoting."""
        note_body = "@testbot /create 'single quotes' \"double quotes\" unquoted"
        result = parse_quick_action(note_body, "testbot")

        assert result is not None
        assert result.command == "create"
        assert result.args == ["single quotes", "double quotes", "unquoted"]

    def test_parse_command_shlex_error_handling(self):
        """Test handling of shlex parsing errors (unmatched quotes)."""
        note_body = '@testbot /create "unmatched quote'
        result = parse_quick_action(note_body, "testbot")

        assert result is None

    def test_parse_empty_note_body(self):
        """Test parsing empty note body."""
        result = parse_quick_action("", "testbot")
        assert result is None

    def test_parse_none_note_body(self):
        """Test parsing None note body."""
        with pytest.raises(TypeError):
            parse_quick_action(None, "testbot")  # type: ignore

    def test_parse_whitespace_only_note_body(self):
        """Test parsing note body with only whitespace."""
        result = parse_quick_action("   \n\t  ", "testbot")
        assert result is None
