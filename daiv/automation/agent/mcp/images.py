"""Images an MCP server returns as base64 inside JSON text instead of as MCP ``ImageContent``.

RT's server sends an attachment as ``{"ContentType": "image/png", "Content": "<base64>"}``. As text the model can
neither see the image nor read it back, so each one is swapped for the path of a file it can open with ``read_file``.
"""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
from typing import Any

_IMAGE_FIELDS = (("ContentType", "Content"), ("mimeType", "data"))


def extract_inline_images(text: str, *, stem: str) -> tuple[str, list[tuple[str, bytes]]]:
    """Replace each base64 image in the JSON ``text`` with the path it should be saved to, ``<stem>-image-<n>.<ext>``.

    Returns the rewritten text and the ``(path, bytes)`` to save, or ``text`` unchanged and no files.
    """
    if "image/" not in text or text.lstrip()[:1] not in ("{", "["):
        return text, []
    try:
        payload = json.loads(text)
    except ValueError:
        return text, []
    files: list[tuple[str, bytes]] = []
    _replace_images(payload, stem, files)
    if not files:
        return text, []
    return json.dumps(payload, ensure_ascii=False), files


def _replace_images(node: Any, stem: str, files: list[tuple[str, bytes]]) -> None:
    if isinstance(node, list):
        for item in node:
            _replace_images(item, stem, files)
        return
    if not isinstance(node, dict):
        return
    for mime_key, data_key in _IMAGE_FIELDS:
        mime_type, encoded = node.get(mime_key), node.get(data_key)
        if not (isinstance(mime_type, str) and mime_type.startswith("image/") and isinstance(encoded, str)):
            continue
        try:
            data = base64.b64decode("".join(encoded.split()), validate=True)
        except binascii.Error:
            continue
        if not data:
            continue
        mime_type = mime_type.split(";")[0].strip()
        path = f"{stem}-image-{len(files) + 1}{mimetypes.guess_extension(mime_type) or ''}"
        files.append((path, data))
        node[data_key] = f"[{mime_type}, {len(data)} bytes, saved to {path}: open it with read_file to view it]"
    for value in node.values():
        _replace_images(value, stem, files)
