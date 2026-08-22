"""The transport package's import boundary, pinned.

``notifications/telegram/`` is the Bot API transport and nothing else: it must not reach into
notification internals, so extracting it to a standalone ``daiv/telegram/`` app later is a move
rather than a rewrite. Nothing else enforces that — one convenience import of
``notifications.models`` would pass every other test in the suite.

The boundary has one carrier this cannot see: ``config.webhook_url`` reverses
``api:telegram_callback`` by name, a route owned by ``notifications/api/views.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from notifications import telegram as telegram_pkg

PACKAGE = "notifications.telegram"
PACKAGE_PARTS = PACKAGE.split(".")
PACKAGE_ROOT = Path(telegram_pkg.__file__).parent
PYTHON_FILES = sorted(PACKAGE_ROOT.rglob("*.py"))


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join([PACKAGE, *parts])


def _package_name(path: Path) -> str:
    """The package a relative import in ``path`` climbs from."""
    module_name = _module_name(path)
    if path.name == "__init__.py":
        return module_name
    return module_name.rsplit(".", 1)[0]


def _resolve(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    parts = package.split(".")
    climbed = parts[: len(parts) - (node.level - 1)]
    return ".".join([*climbed, node.module]) if node.module else ".".join(climbed)


def _imported_modules(path: Path) -> list[str]:
    """Every module this file imports, ``if TYPE_CHECKING:`` blocks included."""
    tree = ast.parse(path.read_text(), filename=str(path))
    package = _package_name(path)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append(_resolve(node, package))
    return found


def test_the_package_still_has_files_to_check():
    # A rename or a move must not silently turn this into a no-op that passes.
    assert "client.py" in [path.name for path in PYTHON_FILES]


@pytest.mark.parametrize("path", PYTHON_FILES, ids=lambda path: path.name)
def test_the_transport_package_never_imports_notification_internals(path):
    offenders = [
        module
        for module in _imported_modules(path)
        if module.split(".")[:1] == ["notifications"] and module.split(".")[: len(PACKAGE_PARTS)] != PACKAGE_PARTS
    ]
    assert offenders == [], (
        f"{path.name} imports {offenders}. notifications/telegram/ is transport-only, so that "
        f"extracting it stays a move; put notification-side logic on the other side of the boundary."
    )
