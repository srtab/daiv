"""Media from tool results: the images and files a tool result carried, kept so the transcript can show them.

``RunToolMediaStore`` is the agent's ``automation.agent.tool_media.ToolMediaStore`` for executor runs: a media block
goes to the ``Run`` that :func:`sessions.artifacts.bind_active_run` names, within the ``sessions.conf`` limits. The
content type comes from an MCP server or ``mimetypes``, so only types a browser can't run script from are kept, and
the endpoint serves them under the artifact sandbox CSP. A kept type has a known file extension, so the content type
the endpoint derives from the stored name is the one kept. ``TranscriptMedia`` turns what the middleware recorded into
what the chat renders, for the live stream and the reloaded transcript alike.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from django.urls import NoReverseMatch, reverse

from asgiref.sync import sync_to_async

from automation.agent.tool_media import (
    DAIV_MEDIA_KEY,
    FAILED,
    RUN_LIMIT,
    TOO_LARGE,
    UNSUPPORTED_TYPE,
    ToolContent,
    ToolMediaError,
)
from sessions.artifacts import aresolve_active_run
from sessions.conf import settings

if TYPE_CHECKING:
    from sessions.models import Run, ToolResultMedia

logger = logging.getLogger("daiv.sessions")

_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_IMAGE_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}
_MEDIA_TYPE = re.compile(r"(audio|video)/[a-z0-9][a-z0-9!#$&^_.+-]*")
_CONTENT_TYPE_MAX_LENGTH = 100
_LINK_SCHEMES = ("http", "https")
# RFC 3986 characters only: a quote, angle bracket or space could break out of the HTML attribute it lands in.
_PLAIN_URL = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")

NOT_SAVED = "not_saved"


class RunToolMediaStore:
    """The executor's ``ToolMediaStore``: a media block goes to the run that ``bind_active_run`` names."""

    @staticmethod
    def content_type(mime_type: str) -> str | None:
        """``mime_type`` normalised (no parameters, lower case) if it may be kept, else ``None``."""
        content_type = mime_type.split(";", 1)[0].strip().lower()
        content_type = _IMAGE_ALIASES.get(content_type, content_type)
        if content_type in _IMAGE_TYPES or content_type == "application/pdf":
            return content_type
        if (
            len(content_type) <= _CONTENT_TYPE_MAX_LENGTH
            and _MEDIA_TYPE.fullmatch(content_type)
            and mimetypes.guess_extension(content_type)
        ):
            return content_type
        return None

    @property
    def max_bytes(self) -> int:
        return settings.TOOL_MEDIA_MAX_BYTES

    async def astore(self, *, thread_id: str, tool_call_id: str, ordinal: int, mime_type: str, content: bytes) -> str:
        if (content_type := self.content_type(mime_type)) is None:
            raise ToolMediaError(UNSUPPORTED_TYPE)
        if len(content) > self.max_bytes:
            raise ToolMediaError(TOO_LARGE)
        if (run := await aresolve_active_run(thread_id)) is None:
            logger.error("Tool media of call %s: no active run on thread %s", tool_call_id, thread_id)
            raise ToolMediaError(FAILED)
        media = await sync_to_async(self._store)(run, tool_call_id, ordinal, content_type, content)
        logger.info(
            "Tool media: run=%s kept %s #%d of call %s (%s, %d bytes)",
            run.pk,
            media.pk,
            ordinal,
            tool_call_id,
            content_type,
            media.size,
        )
        return str(media.pk)

    @staticmethod
    def _store(run: Run, tool_call_id: str, ordinal: int, content_type: str, content: bytes) -> ToolResultMedia:
        from sessions.models import ToolResultMedia

        media = ToolResultMedia(
            run=run, tool_call_id=tool_call_id, ordinal=ordinal, content_type=content_type, size=len(content)
        )
        ToolResultMedia.objects.save_within_run_cap(
            media, "media", content, per_run_max=settings.TOOL_MEDIA_PER_RUN_MAX, error=ToolMediaError(RUN_LIMIT)
        )
        return media


class TranscriptMedia:
    """What the chat renders for the media of one session's tool results: a media URL, an external link, or the reason
    a block was not kept. Only ``http``/``https`` external links survive."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id

    def items(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """An item for each entry the tool-media middleware recorded."""
        return [self._item(entry) for entry in entries]

    def of_message(self, message: Any) -> list[dict[str, Any]]:
        """The :meth:`items` of a checkpointed tool message; a message from before tool media was kept gets a
        ``not_saved`` placeholder per media block instead."""
        entries = (getattr(message, "additional_kwargs", None) or {}).get(DAIV_MEDIA_KEY)
        if entries is not None:
            return self.items(entries)
        return [self._legacy_item(block) for block in ToolContent(message).media_blocks]

    def _item(self, entry: dict[str, Any]) -> dict[str, Any]:
        item: dict[str, Any] = {"kind": entry.get("kind") or "file", "mime_type": entry.get("mime_type") or ""}
        if "size" in entry:
            item["size"] = entry["size"]
        if media_id := entry.get("id"):
            try:
                kwargs = {"thread_id": self.thread_id, "pk": media_id}
                return {**item, "url": reverse("session_tool_media", kwargs=kwargs)}
            except NoReverseMatch:
                logger.error("Tool media %r on thread %r has no URL", media_id, self.thread_id)
                return {**item, "error": FAILED}
        if url := entry.get("url"):
            return {**item, **self._external(url)}
        return {**item, "error": entry.get("error") or FAILED}

    def _legacy_item(self, block: dict[str, Any]) -> dict[str, Any]:
        item = {"kind": block["type"], "mime_type": block.get("mime_type") or ""}
        if isinstance(url := block.get("url"), str) and url and not url.startswith("data:"):
            return {**item, **self._external(url)}
        return {**item, "error": NOT_SAVED}

    @staticmethod
    def _external(url: Any) -> dict[str, Any]:
        if isinstance(url, str) and _PLAIN_URL.fullmatch(url) and urlsplit(url).scheme in _LINK_SCHEMES:
            return {"url": url, "external": True}
        return {"error": UNSUPPORTED_TYPE}
