"""Files an MCP server returns as base64 text instead of as MCP image or resource content.

Embedded in a text result, often a JSON one, such a file is one the model can neither view nor read back. Each base64
run whose decoded bytes are a type ``read_file`` can show is swapped for the path of a file holding them. Detection is
by those bytes alone, never by the server or the JSON keys around them.
"""

from __future__ import annotations

import base64
import binascii
import re

import filetype
from deepagents.backends.utils import _EXTENSION_TO_FILE_TYPE

# JSON-escaped line breaks and slashes are part of a run: a JSON string holding MIME-wrapped base64 carries both.
_BASE64_RUN = re.compile(r"(?:data:[\w.+-]+\\?/[\w.+-]+;base64,)?(?:[A-Za-z0-9+/]|\\[/nr]|\r?\n)+={0,2}")
_LINE_BREAKS = re.compile(r"\\[nr]|\r?\n")
_MIN_BASE64_LENGTH = 128


def extract_embedded_files(text: str, *, stem: str) -> tuple[str, list[tuple[str, bytes]]]:
    """Replace each base64 file in ``text`` that ``read_file`` can show with the path to save it to.

    Paths are ``<stem>-<n>.<ext>``. Returns the rewritten text and the ``(path, bytes)`` to save, or ``text``
    unchanged and no files.
    """
    files: list[tuple[str, bytes]] = []

    def replace(match: re.Match[str]) -> str:
        run = match.group(0)
        decoded = _viewable_file(run.partition(";base64,")[2] or run)
        if decoded is None:
            return run
        data, extension, mime_type = decoded
        path = f"{stem}-{len(files) + 1}.{extension}"
        files.append((path, data))
        return f"[{mime_type}, {len(data)} bytes, saved to {path}: open it with read_file]"

    new_text = _BASE64_RUN.sub(replace, text)
    return (new_text, files) if files else (text, [])


def _viewable_file(encoded: str) -> tuple[bytes, str, str] | None:
    encoded = _LINE_BREAKS.sub("", encoded).replace("\\/", "/")
    if len(encoded) < _MIN_BASE64_LENGTH:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        return None
    kind = filetype.guess(data)
    if kind is None or f".{kind.extension}" not in _EXTENSION_TO_FILE_TYPE:
        return None
    return data, kind.extension, kind.mime
