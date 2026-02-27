"""Unit tests for the command policy evaluator."""

from __future__ import annotations

import pytest

from core.sandbox.command_parser import ExecutableSegment
from core.sandbox.command_policy import CommandPolicy, DenialReason, evaluate_command_policy, parse_rule


def _seg(argv: tuple[str, ...]) -> ExecutableSegment:
    """Helper to build a minimal ExecutableSegment for testing."""
    return ExecutableSegment(argv=argv, raw=" ".join(argv))


class TestParseRule:
    def test_simple_two_token(self):
        assert parse_rule("git commit") == ("git", "commit")

    def test_normalises_to_lowercase(self):
        assert parse_rule("Git Commit") == ("git", "commit")

    def test_extra_whitespace_stripped(self):
        assert parse_rule("  git   push  ") == ("git", "push")

    def test_single_token(self):
        assert parse_rule("rm") == ("rm",)

    def test_empty_string(self):
        assert parse_rule("") == ()


class TestDefaultDisallowRules:
    """Verify that DEFAULT_DISALLOW_RULES blocks the expected built-in cases."""

    @pytest.mark.parametrize(
        "argv",
        [
            ("git", "commit", "-m", "msg"),
            ("git", "push", "origin", "main"),
            ("git", "push", "--force"),
            ("git", "push", "--force-with-lease"),
            ("git", "reset", "--hard"),
            ("git", "reset", "--soft", "HEAD~1"),
            ("git", "rebase", "-i", "HEAD~3"),
            ("git", "clean", "-f"),
            ("git", "clean", "-fd"),
            ("git", "clean", "-fdx"),
            ("git", "branch", "-D", "feature"),
            ("git", "branch", "--delete", "feature"),
            ("git", "tag", "-d", "v1.0"),
            ("git", "tag", "--delete", "v1.0"),
            ("git", "config", "--global", "user.email", "x@y.com"),
            ("git", "config", "--local", "user.name", "Test"),
        ],
    )
    def test_default_disallow_blocks(self, argv):
        result = evaluate_command_policy([_seg(argv)], CommandPolicy())
        assert not result.allowed
        assert result.denial_reason == DenialReason.DEFAULT_DISALLOW

    @pytest.mark.parametrize(
        "argv",
        [
            ("pytest", "tests/"),
            ("python", "-m", "pytest"),
            ("git", "status"),
            ("git", "diff"),
            ("git", "log", "--oneline"),
            ("git", "show", "HEAD"),
            ("git", "fetch", "--dry-run"),
            ("echo", "hello"),
            ("make", "lint"),
            ("ruff", "check", "."),
            ("mypy", "daiv/"),
        ],
    )
    def test_safe_commands_allowed(self, argv):
        result = evaluate_command_policy([_seg(argv)], CommandPolicy())
        assert result.allowed


class TestPrecedence:
    """Verify disallow > allow > default."""

    def test_repo_disallow_blocks_otherwise_allowed(self):
        policy = CommandPolicy(disallow=[("rm", "-rf")])
        segments = [_seg(("rm", "-rf", "/workspace/test"))]
        result = evaluate_command_policy(segments, policy)
        assert not result.allowed
        assert result.denial_reason == DenialReason.REPO_DISALLOW

    def test_repo_allow_does_not_override_default_disallow(self):
        """Allow rules cannot whitelist built-in blocked commands."""
        policy = CommandPolicy(allow=[("git", "commit")])
        segments = [_seg(("git", "commit", "-m", "msg"))]
        result = evaluate_command_policy(segments, policy)
        assert not result.allowed
        assert result.denial_reason == DenialReason.DEFAULT_DISALLOW

    def test_repo_allow_does_not_override_repo_disallow(self):
        """Allow rules cannot override repo-level disallow rules."""
        policy = CommandPolicy(disallow=[("danger", "cmd")], allow=[("danger", "cmd")])
        result = evaluate_command_policy([_seg(("danger", "cmd", "--flag"))], policy)
        assert not result.allowed
        assert result.denial_reason == DenialReason.REPO_DISALLOW

    def test_allow_exempts_from_default_policy(self):
        """An explicit allow rule lets otherwise-acceptable commands through."""
        policy = CommandPolicy(allow=[("mycommand",)])
        result = evaluate_command_policy([_seg(("mycommand", "--run"))], policy)
        assert result.allowed

    def test_empty_policy_uses_defaults(self):
        policy = CommandPolicy()
        # pytest is safe by default
        result = evaluate_command_policy([_seg(("pytest",))], policy)
        assert result.allowed


class TestChainEnforcement:
    """The full invocation is blocked if any segment is denied."""

    def test_safe_plus_denied_blocks_all(self):
        policy = CommandPolicy()
        segments = [_seg(("pytest", "tests")), _seg(("git", "push", "origin", "main"))]
        result = evaluate_command_policy(segments, policy)
        assert not result.allowed

    def test_multiple_safe_all_allowed(self):
        policy = CommandPolicy()
        segments = [_seg(("pytest",)), _seg(("echo", "ok")), _seg(("make", "lint"))]
        result = evaluate_command_policy(segments, policy)
        assert result.allowed

    def test_denied_segment_reported_correctly(self):
        policy = CommandPolicy()
        segments = [_seg(("echo", "safe")), _seg(("git", "commit", "-m", "x"))]
        result = evaluate_command_policy(segments, policy)
        assert not result.allowed
        assert "git" in (result.denied_segment or "")

    def test_first_denial_short_circuits(self):
        """Should return first denial, not necessarily the last."""
        policy = CommandPolicy(disallow=[("first-bad",), ("second-bad",)])
        segments = [_seg(("first-bad",)), _seg(("second-bad",))]
        result = evaluate_command_policy(segments, policy)
        assert not result.allowed
        assert "first-bad" in (result.matched_rule or "")


class TestPolicyResultAttributes:
    def test_denial_has_matched_rule(self):
        policy = CommandPolicy()
        result = evaluate_command_policy([_seg(("git", "commit"))], policy)
        assert result.matched_rule is not None
        assert "git" in result.matched_rule

    def test_allow_has_no_denial_fields(self):
        result = evaluate_command_policy([_seg(("echo", "hi"))], CommandPolicy())
        assert result.allowed
        assert result.denial_reason is None
        assert result.matched_rule is None
        assert result.denied_segment is None

    def test_prefix_matching_longer_argv(self):
        """Rule ("git", "push") should match argv ("git", "push", "--force")."""
        policy = CommandPolicy()
        result = evaluate_command_policy([_seg(("git", "push", "--force"))], policy)
        assert not result.allowed

    def test_partial_prefix_not_matched(self):
        """Rule ("git", "push") should NOT match ("git",) alone."""
        # "git" alone has no sub-command → doesn't match ("git", "push")
        policy = CommandPolicy()
        result = evaluate_command_policy([_seg(("git",))], policy)
        assert result.allowed
