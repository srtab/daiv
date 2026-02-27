"""
Command-level policy evaluation for bash tool invocations.

Provides :func:`evaluate_command_policy` which, given an effective policy and a
parsed list of :class:`~core.sandbox.command_parser.ExecutableSegment` objects,
returns a :class:`PolicyResult` that indicates whether execution is permitted.

Precedence: ``disallow`` rules override ``allow`` rules, which override the
``default_policy``.  Built-in disallow rules are always evaluated first and
cannot be overridden by a repository's ``allow`` list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# Built-in safety defaults
# ---------------------------------------------------------------------------

#: Commands/subcommands that are always blocked regardless of configuration.
#: Entries are tuples of normalized tokens that must match as a *prefix* of the
#: command's argv.  E.g. ("git", "commit") matches ``git commit -m "msg"``.
DEFAULT_DISALLOW_RULES: tuple[tuple[str, ...], ...] = (
    # Git history mutation
    ("git", "commit"),
    ("git", "push"),
    ("git", "reset"),
    ("git", "rebase"),
    ("git", "reflog", "delete"),
    ("git", "filter-branch"),
    ("git", "filter-repo"),
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
    # Force-push variants covered by ("git", "push") prefix above
)


class DenialReason(StrEnum):
    """Machine-readable denial categories for telemetry and error messages."""

    DEFAULT_DISALLOW = "default_disallow"
    REPO_DISALLOW = "repo_disallow"
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
    Effective policy built from all layers: built-in defaults, global settings,
    and per-repository configuration.

    ``disallow`` entries are tuples of normalized tokens (argv prefixes).
    ``allow`` entries follow the same format and whitelist otherwise-denied
    commands.

    Precedence: ``disallow > allow > default policy``.
    Built-in ``DEFAULT_DISALLOW_RULES`` are applied *before* the user-provided
    ``disallow`` list and cannot be overridden by ``allow``.
    """

    disallow: list[tuple[str, ...]] = field(default_factory=list)
    allow: list[tuple[str, ...]] = field(default_factory=list)


def parse_rule(rule: str) -> tuple[str, ...]:
    """
    Convert a space-separated rule string to a normalized token tuple.

    Args:
        rule: A space-separated command prefix, e.g. ``"git commit"``.

    Returns:
        A tuple of lowercased tokens: ``("git", "commit")``.
    """
    return tuple(t.lower() for t in rule.split() if t)


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
    """Lowercase + canonical-flag normalization used for prefix matching."""
    return tuple(_normalize_flag_token(token.lower()) for token in argv)


def _argv_matches_rule(argv: tuple[str, ...], rule: tuple[str, ...]) -> bool:
    """
    Return ``True`` when *argv* starts with the tokens in *rule* (prefix match).

    Comparison is case-insensitive and performs short-flag normalization for
    bundled short options. Example: ``-rf`` and ``-fr`` are considered equal.
    """
    if not rule or len(argv) < len(rule):
        return False
    argv_normalized = _normalize_argv_for_match(argv)
    rule_normalized = _normalize_argv_for_match(rule)
    return argv_normalized[: len(rule_normalized)] == rule_normalized


def _rule_repr(rule: tuple[str, ...]) -> str:
    return " ".join(rule)


def evaluate_command_policy(segments: list, policy: CommandPolicy) -> PolicyResult:
    """
    Evaluate all *segments* against *policy* and return the first denial or an
    allow result.

    The full invocation is blocked when **any** segment is denied (fail-closed).

    Precedence per segment:
    1. Built-in default disallow (cannot be whitelisted by ``allow``).
    2. User ``disallow`` (cannot be overridden by ``allow``).
    3. User ``allow`` (exempts from default policy only, not from 1 or 2).
    4. Default policy → allow (all commands not in DEFAULT_DISALLOW_RULES pass).

    Args:
        segments: List of :class:`~core.sandbox.command_parser.ExecutableSegment`.
        policy: The effective merged policy for this invocation.

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

    # 1. Built-in default disallow (cannot be overridden)
    for rule in DEFAULT_DISALLOW_RULES:
        if _argv_matches_rule(argv, rule):
            return PolicyResult(
                allowed=False,
                denial_reason=DenialReason.DEFAULT_DISALLOW,
                matched_rule=_rule_repr(rule),
                denied_segment=argv_str,
            )

    # 2. Repo/user explicit disallow (cannot be overridden by allow)
    for rule in policy.disallow:
        if _argv_matches_rule(argv, rule):
            return PolicyResult(
                allowed=False,
                denial_reason=DenialReason.REPO_DISALLOW,
                matched_rule=_rule_repr(rule),
                denied_segment=argv_str,
            )

    # 3. Explicit allow list (exempts from default policy only)
    for rule in policy.allow:
        if _argv_matches_rule(argv, rule):
            return PolicyResult(allowed=True)

    # 4. Default policy: allow everything not caught by built-ins or disallow
    return PolicyResult(allowed=True)
