"""
Command-level policy evaluation for bash tool invocations.

Provides :func:`evaluate_command_policy` which, given an effective policy and a
parsed list of :class:`~automation.agent.workspace.bash_policy.command_parser.ExecutableSegment` objects,
returns a :class:`PolicyResult` that indicates whether execution is permitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# Built-in safety defaults
# ---------------------------------------------------------------------------

#: Always blocked; no setting can remove or override them.
DEFAULT_DISALLOW_RULES: tuple[tuple[str, ...], ...] = (
    # Git history mutation
    ("git", "commit"),
    ("git", "push"),
    ("git", "reset"),
    ("git", "rebase"),
    ("git", "reflog", "delete"),
    ("git", "filter-branch"),
    ("git", "filter-repo"),
    # Git index / object manipulation
    ("git", "add"),
    ("git", "hash-object"),
    ("git", "update-index"),
    ("git", "commit-tree"),
    # Destructive working-tree operations
    ("git", "clean"),
    ("git", "checkout", "."),
    ("git", "restore", "."),
    # Branch/tag deletion
    ("git", "branch", "-D"),
    ("git", "branch", "--delete"),
    ("git", "tag", "-d"),
    ("git", "tag", "--delete"),
    # Git configuration changes
    ("git", "config"),
    # Git platform tools
    ("gitlab",),
    ("gh",),
    ("python", "-m", "gitlab"),
)


class DenialReason(StrEnum):
    """Machine-readable denial categories for telemetry and error messages."""

    DEFAULT_DISALLOW = "default_disallow"
    GLOBAL_DISALLOW = "global_disallow"
    PARSE_FAILURE = "parse_failure"


@dataclass(frozen=True)
class PolicyResult:
    """
    The outcome of a policy evaluation.

    Attributes:
        allowed: ``True`` when the command may proceed to sandbox execution.
        denial_reason: The reason category when not allowed.
        matched_rule: A human-readable representation of the matched rule.
        denied_segment: The argv string of the segment that triggered denial.
    """

    allowed: bool
    denial_reason: DenialReason | None = None
    matched_rule: str | None = None
    denied_segment: str | None = None


@dataclass
class CommandPolicy:
    """
    Effective policy: the ``SANDBOX_COMMAND_POLICY_DISALLOW`` and
    ``SANDBOX_COMMAND_POLICY_ALLOW`` settings as token tuples (see :func:`parse_rule`),
    matched like ``DEFAULT_DISALLOW_RULES``.
    """

    disallow: list[tuple[str, ...]] = field(default_factory=list)
    allow: list[tuple[str, ...]] = field(default_factory=list)


def parse_rule(rule: str) -> tuple[str, ...]:
    """
    Convert a space-separated rule string to a lowercased token tuple.

    Args:
        rule: A command name followed by arguments, e.g. ``"git commit"``.

    Returns:
        A tuple of lowercased tokens: ``("git", "commit")``.
    """
    return tuple(t.lower() for t in rule.split())


def _normalize_flag_token(token: str) -> str:
    """
    Canonicalize a token for policy matching.

    For short flag bundles (e.g. ``-rf``), normalize by sorting and deduplicating
    letters so equivalent permutations compare equal (``-rf`` == ``-fr`` == ``-rrf``).

    Notes:
    - Only applies to tokens that look like ``-[A-Za-z]{2,}``.
    - Long options (``--force``), single short options (``-f``), and tokens
      containing non-letters are left unchanged.
    - Duplicate letters are removed: ``-rrf`` → ``-fr``.
    """
    if len(token) >= 3 and token.startswith("-") and not token.startswith("--"):
        short_flags = token[1:]
        if short_flags.isalpha():
            return "-" + "".join(sorted(set(short_flags)))
    return token


def _normalize_argv_for_match(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Lowercase + canonical-flag normalization used for rule matching."""
    return tuple(_normalize_flag_token(token.lower()) for token in argv)


def _argv_matches_rule(argv: tuple[str, ...], rule: tuple[str, ...]) -> bool:
    """
    Return ``True`` when *argv* contains all tokens in *rule* as an in-order
    subsequence, with the rule's first token matching the executable name (argv[0]).

    This handles global flags that appear between the executable name and the
    subcommand, e.g. ``git -C /workspace/repo commit`` is matched by rule
    ``("git", "commit")`` even though ``-C /workspace/repo`` intervenes.

    Comparison is case-insensitive and performs short-flag normalization for
    bundled short options. Example: ``-rf`` and ``-fr`` are considered equal.
    """
    if not rule or not argv:
        return False
    argv_normalized = _normalize_argv_for_match(argv)
    rule_normalized = _normalize_argv_for_match(rule)
    if argv_normalized[0] != rule_normalized[0]:
        return False
    # Remaining rule tokens are matched as an in-order subsequence of the
    # remaining argv tokens, so intervening flags are transparently skipped.
    rule_idx = 1
    for argv_token in argv_normalized[1:]:
        if rule_idx >= len(rule_normalized):
            break
        if argv_token == rule_normalized[rule_idx]:
            rule_idx += 1
    return rule_idx == len(rule_normalized)


def _rule_repr(rule: tuple[str, ...]) -> str:
    return " ".join(rule)


def evaluate_command_policy(segments: list, policy: CommandPolicy) -> PolicyResult:
    """
    Evaluate all *segments* against *policy* and return the first denial or an
    allow result.

    The full invocation is blocked when **any** segment is denied (fail-closed).

    Precedence per segment:
    1. Built-in default disallow (cannot be whitelisted by ``allow``).
    2. Configured ``disallow`` (cannot be overridden by ``allow``).
    3. Configured ``allow`` (returns the same result as 4, so it changes nothing).
    4. Default policy → allow (all commands not caught by 1 or 2 pass).

    Args:
        segments: List of :class:`~automation.agent.workspace.bash_policy.command_parser.ExecutableSegment`.
        policy: The effective policy for this invocation.

    Returns:
        :class:`PolicyResult` with ``allowed=True`` or details of the first
        denial.
    """
    for segment in segments:
        result = _evaluate_segment(segment, policy)
        if not result.allowed:
            return result

    return PolicyResult(allowed=True)


def _evaluate_segment(segment: object, policy: CommandPolicy) -> PolicyResult:
    argv: tuple[str, ...] = getattr(segment, "argv", ())
    argv_str = getattr(segment, "raw", " ".join(str(a) for a in argv))

    if not argv:
        return PolicyResult(allowed=True)

    for rule in DEFAULT_DISALLOW_RULES:
        if _argv_matches_rule(argv, rule):
            return PolicyResult(
                allowed=False,
                denial_reason=DenialReason.DEFAULT_DISALLOW,
                matched_rule=_rule_repr(rule),
                denied_segment=argv_str,
            )

    for rule in policy.disallow:
        if _argv_matches_rule(argv, rule):
            return PolicyResult(
                allowed=False,
                denial_reason=DenialReason.GLOBAL_DISALLOW,
                matched_rule=_rule_repr(rule),
                denied_segment=argv_str,
            )

    for rule in policy.allow:
        if _argv_matches_rule(argv, rule):
            return PolicyResult(allowed=True)

    return PolicyResult(allowed=True)
