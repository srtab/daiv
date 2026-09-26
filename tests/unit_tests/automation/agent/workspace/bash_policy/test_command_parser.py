"""Unit tests for the Parable-backed command parser adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from parable import parse as parable_parse

from automation.agent.workspace.bash_policy.command_parser import CommandParseError, ExecutableSegment, parse_command


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


class TestParseCommandCompound:
    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("if true; then git push; fi", id="if-then"),
            pytest.param("if false; then :; elif true; then git push; fi", id="elif"),
            pytest.param("if false; then :; else git push; fi", id="else"),
            pytest.param("if git push; then :; fi", id="if-condition"),
            pytest.param("case x in y) :;; x) git push;; esac", id="case-arm"),
            pytest.param("echo a; ! git push", id="negation"),
            pytest.param("echo a; time git push", id="time"),
            pytest.param("echo a; coproc git push", id="coproc"),
            pytest.param("while git push; do :; done", id="while-condition"),
            pytest.param("while true; do git push; done", id="while-body"),
            pytest.param("until git push; do :; done", id="until-condition"),
            pytest.param("until false; do git push; done", id="until-body"),
            pytest.param("for x in a; do git push; done", id="for"),
            pytest.param("for ((;;)); do git push; done", id="for-arith"),
            pytest.param("select x in a; do git push; done", id="select"),
            pytest.param("{ git push; }", id="brace-group"),
            pytest.param("( git push )", id="subshell"),
            pytest.param("f() ( git push )", id="function"),
        ],
    )
    def test_nested_command_is_extracted(self, command):
        assert ("git", "push") in [s.argv for s in parse_command(command)]

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("[[ -f x ]] && echo ok", [("echo", "ok")]),
            ("(( i++ )); echo ok", [("echo", "ok")]),
            ("echo a |& tee log", [("echo", "a"), ("tee", "log")]),
        ],
    )
    def test_leaf_constructs_yield_no_segment(self, command, expected):
        assert [s.argv for s in parse_command(command)] == expected

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("time pytest", [("pytest",)]),
            ("! grep x f", [("grep", "x", "f")]),
            ("case x in x) echo ok;; esac", [("echo", "ok")]),
            ("coproc echo hi", [("echo", "hi")]),
        ],
    )
    def test_lone_compound_command_is_parsed(self, command, expected):
        assert [s.argv for s in parse_command(command)] == expected

    @pytest.mark.parametrize(
        ("kind", "match"),
        [
            pytest.param("new-construct", "unsupported shell construct: new-construct", id="unknown-kind"),
            pytest.param("subshell", "subshell node has no 'body' field", id="missing-child-field"),
            pytest.param("command", "command node has no 'words' field", id="missing-words-field"),
        ],
    )
    def test_parable_drift_fails_closed(self, monkeypatch, kind, match):
        nodes = [*parable_parse("echo a"), SimpleNamespace(kind=kind)]
        monkeypatch.setattr(
            "automation.agent.workspace.bash_policy.command_parser._parable_parse", lambda _command: nodes
        )
        with pytest.raises(CommandParseError, match=match):
            parse_command("echo a; <drifted node>")


class TestParseCommandAssignments:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("HUSKY=0 git commit -m x", ("git", "commit", "-m", "x")),
            ("A=1 B[0]=2 C+=3 git push", ("git", "push")),
            ("a[b[1]]=2 git push", ("git", "push")),
            ("make CC=gcc", ("make", "CC=gcc")),
            ('"FOO=1" git push', ('"FOO=1"', "git", "push")),
            ("FOO=1", ()),
        ],
    )
    def test_leading_assignments_are_dropped_from_argv(self, command, expected):
        assert parse_command(command)[0].argv == expected

    def test_raw_keeps_leading_assignments(self):
        assert parse_command("HUSKY=0 git push")[0].raw == "HUSKY=0 git push"


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
