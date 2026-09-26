"""
Parable-backed adapter for parsing bash command strings into executable segments.

This module is the single point of integration with the Parable library.
All Parable-specific AST handling lives here; the rest of the codebase depends
only on the stable :class:`ExecutableSegment` interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import dropwhile

from parable import ParseError as ParableParseError
from parable import _looks_like_assignment
from parable import parse as _parable_parse


@dataclass(frozen=True)
class ExecutableSegment:
    """
    A normalized representation of one executable command within a compound input.

    Attributes:
        argv: The command name and its arguments, with leading variable assignments
            dropped, so an assignment-only command (``FOO=1``) has an empty argv.
        raw: All of the command's words joined by spaces (for logging / diagnostics only).
    """

    argv: tuple[str, ...]
    raw: str

    @property
    def name(self) -> str:
        """The executable name (argv[0])."""
        return self.argv[0] if self.argv else ""


class CommandParseError(Exception):
    """Raised when Parable cannot parse the given command string."""

    def __init__(self, command: str, reason: str) -> None:
        self.command = command
        self.reason = reason
        super().__init__(f"Failed to parse command: {reason}")


def parse_command(command: str) -> list[ExecutableSegment]:
    """
    Parse *command* into a flat list of executable segments using Parable.

    Each segment corresponds to one simple command reachable in the execution
    graph (across ``&&``, ``||``, ``;``, ``|`` operators and all nesting
    levels except substitutions).  The caller should evaluate **every** segment
    before allowing execution.

    Args:
        command: The raw bash command string to parse.

    Returns:
        A non-empty list of :class:`ExecutableSegment` instances.

    Raises:
        CommandParseError: If Parable cannot parse the command, the result holds a
            construct the walker does not know, or no executable command is found.
    """
    if not command or not command.strip():
        raise CommandParseError(command, "empty command")

    try:
        ast_nodes = _parable_parse(command)
    except ParableParseError as exc:
        raise CommandParseError(command, str(exc)) from exc
    except Exception as exc:
        raise CommandParseError(command, f"unexpected parser error: {exc}") from exc

    segments: list[ExecutableSegment] = []
    for node in ast_nodes:
        _walk(node, command, segments)

    if not segments:
        raise CommandParseError(command, "no executable commands found after parsing")

    return segments


# ---------------------------------------------------------------------------
# Internal AST traversal helpers
# ---------------------------------------------------------------------------


_CHILD_FIELDS: dict[str, tuple[str, ...]] = {
    "pipeline": ("commands",),
    "list": ("parts",),
    "if": ("condition", "then_body", "else_body"),
    "while": ("condition", "body"),
    "until": ("condition", "body"),
    "for": ("body",),
    "for-arith": ("body",),
    "select": ("body",),
    "case": ("patterns",),
    "pattern": ("body",),
    "brace-group": ("body",),
    "subshell": ("body",),
    "function": ("body",),
    "negation": ("pipeline",),
    "time": ("pipeline",),
    "coproc": ("command",),
}

#: Kinds with no child command nodes; listing a kind that has them hides those commands from the policy.
_LEAF_KINDS = frozenset({"operator", "pipe-both", "empty", "comment", "redirect", "arith-cmd", "cond-expr"})


def _walk(node: object, source: str, out: list[ExecutableSegment]) -> None:
    """
    Dispatch on node kind.

    Raises:
        CommandParseError: On an unknown kind or a missing field, so Parable drift blocks commands.
    """
    kind = getattr(node, "kind", None)

    if kind == "command":
        words = tuple(_field(word, "value", source) for word in _field(node, "words", source))
        if words:
            argv = tuple(dropwhile(_looks_like_assignment, words))
            out.append(ExecutableSegment(argv=argv, raw=" ".join(words)))
    elif kind in _CHILD_FIELDS:
        for attr in _CHILD_FIELDS[kind]:
            child = _field(node, attr, source)
            for item in child if isinstance(child, list) else (child,):
                if item is not None:
                    _walk(item, source, out)
    elif kind not in _LEAF_KINDS:
        raise CommandParseError(source, f"unsupported shell construct: {kind}")


def _field(node: object, attr: str, source: str):
    try:
        return getattr(node, attr)
    except AttributeError:
        raise CommandParseError(source, f"{getattr(node, 'kind', None)} node has no {attr!r} field") from None
