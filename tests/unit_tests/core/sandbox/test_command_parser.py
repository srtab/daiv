"""Unit tests for the Parable-backed command parser adapter."""

from __future__ import annotations

import pytest

from core.sandbox.command_parser import CommandParseError, ExecutableSegment, parse_command


class TestParseCommandBasic:
    def test_single_simple_command(self):
        segments = parse_command("pytest tests/")
        assert len(segments) == 1
        assert segments[0].name == "pytest"
        assert segments[0].argv == ("pytest", "tests/")

    def test_command_with_flags(self):
        segments = parse_command("git status --short")
        assert segments[0].name == "git"
        assert "status" in segments[0].argv

    def test_git_diff(self):
        segments = parse_command("git diff HEAD")
        assert len(segments) == 1
        assert segments[0].argv[:2] == ("git", "diff")

    def test_empty_command_raises(self):
        with pytest.raises(CommandParseError):
            parse_command("")

    def test_whitespace_only_raises(self):
        with pytest.raises(CommandParseError):
            parse_command("   ")

    def test_unmatched_quote_raises(self):
        with pytest.raises(CommandParseError):
            parse_command('echo "unclosed')

    def test_executable_segment_name_property(self):
        seg = ExecutableSegment(argv=("git", "status"), raw="git status")
        assert seg.name == "git"

    def test_executable_segment_empty_argv_name(self):
        seg = ExecutableSegment(argv=(), raw="")
        assert seg.name == ""


class TestParseCommandChaining:
    def test_and_chain_yields_two_segments(self):
        segments = parse_command("pytest tests && echo done")
        names = [s.name for s in segments]
        assert "pytest" in names
        assert "echo" in names

    def test_semicolon_chain_yields_two_segments(self):
        segments = parse_command("git status; echo ok")
        names = [s.name for s in segments]
        assert "git" in names
        assert "echo" in names

    def test_pipe_chain_yields_two_segments(self):
        segments = parse_command("ps aux | grep python")
        names = [s.name for s in segments]
        assert "ps" in names
        assert "grep" in names

    def test_or_chain_yields_two_segments(self):
        segments = parse_command("make lint || echo failed")
        names = [s.name for s in segments]
        assert "make" in names
        assert "echo" in names

    def test_complex_chain_all_segments_found(self):
        segments = parse_command("pytest tests && git status; echo done")
        names = [s.name for s in segments]
        assert "pytest" in names
        assert "git" in names
        assert "echo" in names

    def test_triple_pipe_chain(self):
        segments = parse_command("ps aux | grep python | awk '{print $2}'")
        assert len(segments) == 3
        assert segments[0].name == "ps"
        assert segments[1].name == "grep"
        assert segments[2].name == "awk"

    def test_hidden_disallowed_subcommand(self):
        segments = parse_command("pytest tests && git push origin main")
        git_segments = [s for s in segments if s.name == "git"]
        assert any("push" in s.argv for s in git_segments)

    def test_inject_via_semicolon(self):
        segments = parse_command("echo safe; git commit -m test")
        names = [s.name for s in segments]
        assert "git" in names

    def test_inject_via_double_ampersand(self):
        segments = parse_command("echo safe && git reset --hard")
        names = [s.name for s in segments]
        assert "git" in names


class TestParseCommandErrorHandling:
    def test_parse_error_carries_reason(self):
        with pytest.raises(CommandParseError) as exc_info:
            parse_command('echo "unclosed')
        assert exc_info.value.reason

    def test_parse_error_carries_command(self):
        cmd = 'echo "bad'
        with pytest.raises(CommandParseError) as exc_info:
            parse_command(cmd)
        assert exc_info.value.command == cmd
