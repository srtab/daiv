import pytest

from slash_commands.parser import parse_slash_command


class TestParseSlashCommand:
    def test_parse_simple_command(self):
        """Test parsing a simple bot command."""
        note_body = "@testbot /help"
        result = parse_slash_command(note_body, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.args == []
        assert result.raw == "@testbot /help"

    def test_parse_command_with_args(self):
        """Test parsing a command with arguments."""
        note_body = "@testbot /assign user1 user2"
        result = parse_slash_command(note_body, "testbot")

        assert result is not None
        assert result.command == "assign"
        assert result.args == ["user1", "user2"]
        assert result.raw == "@testbot /assign user1 user2"

    def test_parse_command_with_quoted_args(self):
        """Test parsing a command with quoted arguments."""
        note_body = '@testbot /create "test issue" --label "bug fix"'
        result = parse_slash_command(note_body, "testbot")

        assert result is not None
        assert result.command == "create"
        assert result.args == ["test issue", "--label", "bug fix"]
        assert result.raw == '@testbot /create "test issue" --label "bug fix"'

    def test_parse_case_insensitive_bot_name(self):
        """Test that bot name matching is case insensitive."""
        note_body = "@TestBot /help"
        result = parse_slash_command(note_body, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.raw == "@TestBot /help"

    def test_parse_case_insensitive_command(self):
        """Test that command is converted to lowercase."""
        note_body = "@testbot /HELP"
        result = parse_slash_command(note_body, "testbot")

        assert result is not None
        assert result.command == "help"  # Should be lowercase

    def test_parse_command_in_middle_of_text(self):
        """Test parsing command that appears in middle of note."""
        note_body = "Some text before\n@testbot /help\nSome text after"
        result = parse_slash_command(note_body, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.raw == "@testbot /help"

    def test_parse_first_command_only(self):
        """Test that only first command is parsed when multiple exist."""
        note_body = "@testbot /help\n@testbot /status"
        result = parse_slash_command(note_body, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.raw == "@testbot /help"

    def test_parse_no_command_found(self):
        """Test when no bot command is found."""
        note_body = "Just some regular text without commands"
        result = parse_slash_command(note_body, "testbot")

        assert result is None

    def test_parse_different_bot_name(self):
        """Test that commands for different bots are ignored."""
        note_body = "@otherbot /help"
        result = parse_slash_command(note_body, "testbot")

        assert result is None

    def test_parse_email_address_ignored(self):
        """Test that email addresses are not matched as bot commands."""
        note_body = "Contact testbot@example.com for help"
        result = parse_slash_command(note_body, "testbot")

        assert result is None

    def test_parse_partial_bot_name_ignored(self):
        """Test that partial bot name matches are ignored."""
        note_body = "@testbotx /help"  # Extra character
        result = parse_slash_command(note_body, "testbot")

        assert result is None

    def test_parse_bot_name_with_special_chars(self):
        """Test parsing bot name that contains special regex characters."""
        note_body = "@test.bot /help"
        result = parse_slash_command(note_body, "test.bot")

        assert result is not None
        assert result.command == "help"

    def test_parse_command_with_newline_in_middle(self):
        """Test that commands stop at newlines."""
        note_body = "@testbot /help arg1\nthis should not be included"
        result = parse_slash_command(note_body, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.args == ["arg1"]
        assert "this should not be included" not in result.raw

    def test_parse_empty_command(self):
        """Test parsing when bot is mentioned but no command follows."""
        note_body = "@testbot   "  # Just whitespace after mention
        result = parse_slash_command(note_body, "testbot")

        assert result is None

    def test_parse_command_with_tabs(self):
        """Test parsing command with tab characters."""
        note_body = "@testbot\t\t/help\targ1"
        result = parse_slash_command(note_body, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.args == ["arg1"]

    def test_parse_command_with_complex_quoting(self):
        """Test parsing command with complex shell-style quoting."""
        note_body = "@testbot /create 'single quotes' \"double quotes\" unquoted"
        result = parse_slash_command(note_body, "testbot")

        assert result is not None
        assert result.command == "create"
        assert result.args == ["single quotes", "double quotes", "unquoted"]

    def test_parse_command_shlex_error_handling(self):
        """Test handling of shlex parsing errors (unmatched quotes)."""
        note_body = '@testbot /create "unmatched quote'
        result = parse_slash_command(note_body, "testbot")

        assert result is None

    def test_parse_empty_note_body(self):
        """Test parsing empty note body."""
        result = parse_slash_command("", "testbot")
        assert result is None

    def test_parse_none_note_body(self):
        """Test parsing None note body."""
        with pytest.raises(TypeError):
            parse_slash_command(None, "testbot")  # type: ignore

    def test_parse_whitespace_only_note_body(self):
        """Test parsing note body with only whitespace."""
        result = parse_slash_command("   \n\t  ", "testbot")
        assert result is None

    def test_parse_bare_slash_command(self):
        """Test parsing bare slash command (/command)."""
        text = "/help"
        result = parse_slash_command(text, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.args == []
        assert result.raw == "/help"

    def test_parse_bare_slash_command_with_args(self):
        """Test parsing bare slash command with arguments."""
        text = "/review please check the security aspects"
        result = parse_slash_command(text, "testbot")

        assert result is not None
        assert result.command == "review"
        assert result.args == ["please", "check", "the", "security", "aspects"]
        assert result.raw == "/review please check the security aspects"

    def test_parse_bare_slash_command_with_leading_whitespace(self):
        """Test parsing bare slash command with leading whitespace."""
        text = "  /help"
        result = parse_slash_command(text, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.args == []
        assert "/help" in result.raw

    def test_parse_bare_slash_command_in_multiline(self):
        """Test parsing bare slash command in multiline text."""
        text = "Some text before\n/help\nSome text after"
        result = parse_slash_command(text, "testbot")

        assert result is None

    def test_parse_prioritizes_mention_over_bare(self):
        """Test that mention format is prioritized over bare format."""
        text = "/bare-command\n@testbot /mention-command"
        result = parse_slash_command(text, "testbot")

        # Should find the mention format first
        assert result is not None
        assert result.command == "mention-command"

    def test_parse_bare_slash_command_with_quoted_args(self):
        """Test parsing bare slash command with quoted arguments."""
        text = '/security-audit "check authentication"'
        result = parse_slash_command(text, "testbot")

        assert result is not None
        assert result.command == "security-audit"
        assert result.args == ["check authentication"]

    def test_parse_bare_slash_at_start_of_line(self):
        """Test parsing bare slash command at the start of a line."""
        text = "/help me understand"
        result = parse_slash_command(text, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.args == ["me", "understand"]

    def test_parse_bare_slash_not_mid_word(self):
        """Test that slash in middle of word is not detected."""
        text = "http://example.com/help"
        result = parse_slash_command(text, "testbot")

        assert result is None

    def test_parse_bare_slash_command_case_insensitive(self):
        """Test that bare slash commands are converted to lowercase."""
        text = "/HELP"
        result = parse_slash_command(text, "testbot")

        assert result is not None
        assert result.command == "help"

    def test_parse_bare_slash_shlex_error(self):
        """Test handling of shlex parsing errors in bare format."""
        text = '/command "unmatched quote'
        result = parse_slash_command(text, "testbot")

        assert result is None

    def test_parse_bare_slash_only(self):
        """Test parsing when only slash is present."""
        text = "/"
        result = parse_slash_command(text, "testbot")

        # Should return None as there's no command after the slash
        assert result is None

    def test_parse_bare_slash_with_tabs(self):
        """Test parsing bare slash command with tabs."""
        text = "/help\targ1\targ2"
        result = parse_slash_command(text, "testbot")

        assert result is not None
        assert result.command == "help"
        assert result.args == ["arg1", "arg2"]
