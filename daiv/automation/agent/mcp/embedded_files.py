"""Files an MCP server returns as base64 text instead of as MCP image or resource content.

Embedded in a text result, often a JSON one, such a file is one the model can neither view nor read back. Each base64
run whose decoded bytes are a type ``read_file`` can show is saved to a file and swapped for its path. Detection is by
those bytes alone, never by the server or the JSON keys around them.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from typing import TYPE_CHECKING

import filetype
from deepagents.backends.utils import _get_file_type
from langchain_core.tools import StructuredTool

from automation.agent.middlewares.file_system import large_tool_results_prefix

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from langchain_core.tools.base import BaseTool

logger = logging.getLogger("daiv.tools")

# JSON-escaped line breaks and slashes are part of a run: a JSON string holding MIME-wrapped base64 carries both.
_BASE64_RUN = re.compile(r"(?:data:[\w.+-]+\\?/[\w.+-]+;base64,)?((?:[A-Za-z0-9+/]++|\\[/nr]|\r?\n)++={0,2})")
# A cheap pre-scan; shorter than a 76-character MIME line, so wrapped base64 still passes it.
_LONG_RUN = re.compile(r"[A-Za-z0-9+/\\]{64}")
_MIN_BASE64_LENGTH = 128


def save_embedded_files(tool: BaseTool, backend: BackendProtocol) -> None:
    """Make ``tool`` save the files its results embed as base64 text under ``backend``, leaving their paths.

    A result whose files fail to save comes back as it was.
    """
    if not isinstance(tool, StructuredTool) or tool.coroutine is None:
        raise TypeError(f"langchain.mcp built tool {tool.name!r} in an unexpected shape")
    call, directory = tool.coroutine, large_tool_results_prefix(backend)

    async def coroutine(**arguments):
        content, artifact = await call(**arguments)
        files: dict[str, bytes] = {}
        rewritten = []
        for block in content:
            if block.get("type") == "text":
                text, found = extract_embedded_files(block["text"], directory=directory)
                block = {**block, "text": text}
                files.update(found)
            rewritten.append(block)
        return (rewritten if files and await _upload(backend, files) else content), artifact

    tool.coroutine = coroutine


def extract_embedded_files(text: str, *, directory: str) -> tuple[str, dict[str, bytes]]:
    """Replace each base64 file in ``text`` that ``read_file`` can show with the path to save it to under ``directory``.

    Paths are named by content, so a file embedded twice is saved once. Returns the rewritten text and the files.
    """
    files: dict[str, bytes] = {}
    if _LONG_RUN.search(text) is None:
        return text, files

    def replace(match: re.Match[str]) -> str:
        if match.end(1) - match.start(1) < _MIN_BASE64_LENGTH or (decoded := _viewable_file(match[1])) is None:
            return match[0]
        data, kind = decoded
        path = f"{directory}/{hashlib.sha256(data).hexdigest()[:16]}.{kind.extension}"
        files[path] = data
        return f"[{kind.mime}, {len(data)} bytes, saved to {path}: open it with read_file]"

    return _BASE64_RUN.sub(replace, text), files


def _viewable_file(encoded: str) -> tuple[bytes, filetype.Type] | None:
    for escape in ("\\n", "\\r", "\n", "\r"):
        encoded = encoded.replace(escape, "")
    try:
        data = base64.b64decode(encoded.replace("\\/", "/"), validate=True)
    except binascii.Error:
        return None
    kind = filetype.guess(data)
    if kind is None or _get_file_type(f"file.{kind.extension}") == "text":
        return None
    return data, kind


async def _upload(backend: BackendProtocol, files: dict[str, bytes]) -> bool:
    try:
        failures = [response.error for response in await backend.aupload_files(list(files.items())) if response.error]
    except Exception:
        logger.exception("Failed to save %d embedded file(s)", len(files))
        return False
    if failures:
        logger.error("Failed to save embedded files: %s", failures)
    return not failures
