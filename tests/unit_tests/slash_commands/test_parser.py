import pytest

from slash_commands.parser import parse_slash_command


def _assert_parsed(note_body: str, bot_name: str, *, command: str, args: list[str], raw: str) -> None:
    result = parse_slash_command(note_body, bot_name)

    assert result is not None
    assert result.command == command
    assert result.args == args
    assert result.raw == raw


def _assert_not_parsed(note_body: str, bot_name: str) -> None:
    assert parse_slash_command(note_body, bot_name) is None


MENTION_CASES = [
    pytest.param("@testbot /help", "testbot", "help", [], "@testbot /help", id="simple"),
    pytest.param(
        "@testbot /assign user1 user2",
        "testbot",
        "assign",
        ["user1", "user2"],
        "@testbot /assign user1 user2",
        id="args",
    ),
    pytest.param(
        '@testbot /create "test issue" --label "bug fix"',
        "testbot",
        "create",
        ["test issue", "--label", "bug fix"],
        '@testbot /create "test issue" --label "bug fix"',
        id="quoted",
    ),
    pytest.param("@TestBot /help", "testbot", "help", [], "@TestBot /help", id="case-insensitive-bot"),
    pytest.param("@testbot /HELP", "testbot", "help", [], "@testbot /HELP", id="case-insensitive-command"),
    pytest.param(
        "Some text before\n@testbot /help\nSome text after",
        "testbot",
        "help",
        [],
        "@testbot /help",
        id="middle-of-text",
    ),
    pytest.param("@testbot /help\n@testbot /status", "testbot", "help", [], "@testbot /help", id="first-command-only"),
    pytest.param("@test.bot /help", "test.bot", "help", [], "@test.bot /help", id="special-chars-bot"),
    pytest.param(
        "@testbot /help arg1\nthis should not be included",
        "testbot",
        "help",
        ["arg1"],
        "@testbot /help arg1",
        id="newline-stops",
    ),
    pytest.param("@testbot\t\t/help\targ1", "testbot", "help", ["arg1"], "@testbot\t\t/help\targ1", id="tabs"),
    pytest.param(
        "@testbot /create 'single quotes' \"double quotes\" unquoted",
        "testbot",
        "create",
        ["single quotes", "double quotes", "unquoted"],
        "@testbot /create 'single quotes' \"double quotes\" unquoted",
        id="complex-quoting",
    ),
]

BARE_CASES = [
    pytest.param("/help", "testbot", "help", [], "/help", id="bare-simple"),
    pytest.param(
        "/review please check the security aspects",
        "testbot",
        "review",
        ["please", "check", "the", "security", "aspects"],
        "/review please check the security aspects",
        id="bare-args",
    ),
    pytest.param("  /help", "testbot", "help", [], "/help", id="bare-leading-whitespace"),
    pytest.param(
        '/security-audit "check authentication"',
        "testbot",
        "security-audit",
        ["check authentication"],
        '/security-audit "check authentication"',
        id="bare-quoted",
    ),
    pytest.param(
        "/help me understand", "testbot", "help", ["me", "understand"], "/help me understand", id="bare-start-line"
    ),
    pytest.param("/HELP", "testbot", "help", [], "/HELP", id="bare-case-insensitive"),
    pytest.param("/help\targ1\targ2", "testbot", "help", ["arg1", "arg2"], "/help\targ1\targ2", id="bare-tabs"),
]

INVALID_CASES = [
    pytest.param("Just some regular text without commands", "testbot", id="no-command"),
    pytest.param("@otherbot /help", "testbot", id="different-bot"),
    pytest.param("Contact testbot@example.com for help", "testbot", id="email"),
    pytest.param("@testbotx /help", "testbot", id="partial-bot"),
    pytest.param("@testbot   ", "testbot", id="mention-no-command"),
    pytest.param("", "testbot", id="empty-note"),
    pytest.param("   \n\t  ", "testbot", id="whitespace-only"),
    pytest.param("http://example.com/help", "testbot", id="slash-mid-word"),
    pytest.param("Some text before\n/help\nSome text after", "testbot", id="bare-multiline"),
    pytest.param("/", "testbot", id="bare-slash-only"),
]

INVALID_SHLEX_CASES = [
    pytest.param('@testbot /create "unmatched quote', "testbot", id="mention-unmatched-quote"),
    pytest.param('/command "unmatched quote', "testbot", id="bare-unmatched-quote"),
]


@pytest.mark.parametrize("note_body, bot_name, command, args, raw", MENTION_CASES)
def test_parse_mention_command_cases(note_body: str, bot_name: str, command: str, args: list[str], raw: str) -> None:
    """Test parsing mention-based slash commands."""
    _assert_parsed(note_body, bot_name, command=command, args=args, raw=raw)


@pytest.mark.parametrize("note_body, bot_name, command, args, raw", BARE_CASES)
def test_parse_bare_command_cases(note_body: str, bot_name: str, command: str, args: list[str], raw: str) -> None:
    """Test parsing bare slash commands."""
    _assert_parsed(note_body, bot_name, command=command, args=args, raw=raw)


@pytest.mark.parametrize("note_body, bot_name", INVALID_CASES)
def test_parse_invalid_cases(note_body: str, bot_name: str) -> None:
    """Test inputs that should not parse into commands."""
    _assert_not_parsed(note_body, bot_name)


@pytest.mark.parametrize("note_body, bot_name", INVALID_SHLEX_CASES)
def test_parse_shlex_error_handling(note_body: str, bot_name: str) -> None:
    """Test handling of shlex parsing errors (unmatched quotes)."""
    _assert_not_parsed(note_body, bot_name)


def test_parse_prioritizes_mention_over_bare() -> None:
    """Test that mention format is prioritized over bare format."""
    text = "/bare-command\n@testbot /mention-command"
    result = parse_slash_command(text, "testbot")

    assert result is not None
    assert result.command == "mention-command"


def test_parse_none_note_body() -> None:
    """Test parsing None note body."""
    with pytest.raises(TypeError):
        parse_slash_command(None, "testbot")
