"""
Parable-backed adapter for parsing bash command strings into executable segments.

This module is the single point of integration with the Parable library.
All Parable-specific AST handling lives here; the rest of the codebase depends
only on the stable :class:`ExecutableSegment` interface.
"""

from __future__ import annotations

from dataclasses import dataclass

from parable import ParseError as ParableParseError
from parable import parse as _parable_parse


@dataclass(frozen=True)
class ExecutableSegment:
    """
    A normalized representation of one executable command within a compound input.

    Attributes:
        argv: The command name followed by its arguments, derived from the parsed
            Word tokens. The first element is the executable name.
        raw: The original un-parsed source slice (for logging / diagnostics only).
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
    levels).  The caller should evaluate **every** segment before allowing
    execution.

    Args:
        command: The raw bash command string to parse.

    Returns:
        A non-empty list of :class:`ExecutableSegment` instances.

    Raises:
        CommandParseError: If Parable cannot parse the command or if the result
            is ambiguous / empty in a way that prevents safe analysis.
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
    _collect_segments(ast_nodes, command, segments)

    if not segments:
        raise CommandParseError(command, "no executable commands found after parsing")

    return segments


# ---------------------------------------------------------------------------
# Internal AST traversal helpers
# ---------------------------------------------------------------------------


def _collect_segments(nodes: list, source: str, out: list[ExecutableSegment]) -> None:
    """Recursively walk Parable AST nodes and populate *out* with segments."""
    for node in nodes:
        _walk(node, source, out)


def _walk(node: object, source: str, out: list[ExecutableSegment]) -> None:
    """Dispatch on node kind."""
    kind = getattr(node, "kind", None)

    if kind == "command":
        _handle_command(node, out)

    elif kind == "pipeline":
        for child in node.commands:
            _walk(child, source, out)

    elif kind in {"list"}:
        for part in node.parts:
            _walk(part, source, out)

    elif kind in {
        "if",
        "while",
        "until",
        "for",
        "for-arith",
        "select",
        "case",
        "brace-group",
        "subshell",
        "arith-command",
        "cond-expr",
        "time",
        "negation",
        "coproc",
        "function",
    }:
        _walk_compound(node, source, out)

    # "operator", "empty", "comment", "redirect", etc. are intentionally ignored


def _handle_command(node: object, out: list[ExecutableSegment]) -> None:
    """Convert a Command node into an ExecutableSegment."""
    words = getattr(node, "words", [])
    if not words:
        return

    argv_parts: list[str] = []
    for word in words:
        val = getattr(word, "value", None)
        if val is not None:
            argv_parts.append(val)

    if not argv_parts:
        return

    out.append(ExecutableSegment(argv=tuple(argv_parts), raw=" ".join(argv_parts)))


def _walk_compound(node: object, source: str, out: list[ExecutableSegment]) -> None:
    """
    Walk compound constructs by visiting all child attributes that may contain
    executable nodes (body, condition, then_part, else_part, etc.).
    """
    for attr in (
        "body",
        "condition",
        "then_part",
        "elif_clauses",
        "else_part",
        "commands",
        "parts",
        "list",
        "items",
        "pattern_list",
    ):
        child = getattr(node, attr, None)
        if child is None:
            continue
        if isinstance(child, list):
            for item in child:
                if hasattr(item, "kind"):
                    _walk(item, source, out)
                elif hasattr(item, "__iter__"):
                    for sub in item:
                        if hasattr(sub, "kind"):
                            _walk(sub, source, out)
        elif hasattr(child, "kind"):
            _walk(child, source, out)
