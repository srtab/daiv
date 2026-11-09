"""Media from tool results: the images and files a tool result carried, kept so the transcript can show them.

``RunToolMediaStore`` is the agent's ``automation.agent.tool_media.ToolMediaStore`` for executor runs: a media block
goes to the ``Run`` that :func:`sessions.artifacts.bind_active_run` names, within the ``sessions.conf`` limits. The
content type comes from an MCP server or ``mimetypes``, so only types a browser can't run script from are kept, and
the endpoint serves them under the artifact sandbox CSP.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import NoReverseMatch, reverse

from asgiref.sync import sync_to_async

from automation.agent.tool_media import (
    DAIV_MEDIA_KEY,
    FAILED,
    RUN_LIMIT,
    TOO_LARGE,
    UNSUPPORTED_TYPE,
    ToolMediaError,
    media_blocks,
)
from sessions.artifacts import aresolve_active_run, delete_stored_file
from sessions.conf import settings

if TYPE_CHECKING:
    from sessions.models import Run, ToolResultMedia

logger = logging.getLogger("daiv.sessions")

_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_LINK_SCHEMES = ("http", "https")

NOT_SAVED = "not_saved"


def stored_content_type(mime_type: str) -> str | None:
    """``mime_type`` normalised (no parameters, lower case) if it may be kept, else ``None``."""
    content_type = mime_type.split(";", 1)[0].strip().lower()
    if content_type in _IMAGE_TYPES or content_type == "application/pdf":
        return content_type
    major, _, minor = content_type.partition("/")
    return content_type if major in ("audio", "video") and minor else None


class RunToolMediaStore:
    """The executor's ``ToolMediaStore``: a media block goes to the run that ``bind_active_run`` names."""

    async def astore(self, *, thread_id: str, tool_call_id: str, ordinal: int, mime_type: str, content: bytes) -> str:
        if (run := await aresolve_active_run(thread_id)) is None:
            logger.warning("Tool media of call %s: no active run on thread %s", tool_call_id, thread_id)
            raise ToolMediaError(FAILED)
        if (content_type := stored_content_type(mime_type)) is None:
            raise ToolMediaError(UNSUPPORTED_TYPE)
        if len(content) > settings.TOOL_MEDIA_MAX_BYTES:
            raise ToolMediaError(TOO_LARGE)
        media = await sync_to_async(_store_media)(run, tool_call_id, ordinal, content_type, content)
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


def _store_media(run: Run, tool_call_id: str, ordinal: int, content_type: str, content: bytes) -> ToolResultMedia:
    from sessions.models import Run, ToolResultMedia

    media = ToolResultMedia(
        run=run, tool_call_id=tool_call_id, ordinal=ordinal, content_type=content_type, size=len(content)
    )
    try:
        media.file.save("media", ContentFile(content), save=False)
        with transaction.atomic():
            # Parallel tool calls in one model turn store concurrently; the lock serialises the budget check.
            Run.objects.select_for_update().only("pk").get(pk=run.pk)
            if ToolResultMedia.objects.filter(run=run).count() >= settings.TOOL_MEDIA_PER_RUN_MAX:
                raise ToolMediaError(RUN_LIMIT)
            media.save()
    except BaseException:
        name = media.file.name or media.file.field.generate_filename(media, "media")
        delete_stored_file(media.file.storage, name)
        raise
    return media


def tool_text(content: Any) -> str:
    """The text of a tool result's ``content``: its text blocks and bare strings, one per line."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    return "\n".join(
        block if isinstance(block, str) else str(block.get("text", ""))
        for block in content
        if isinstance(block, str) or (isinstance(block, dict) and block.get("type") == "text")
    )


def media_items(entries: list[dict[str, Any]], *, thread_id: str) -> list[dict[str, Any]]:
    """What the chat renders for each entry the tool-media middleware recorded: a media URL, an external link, or the
    reason it was not kept. Only ``http``/``https`` external links survive."""
    return [_item(entry, thread_id) for entry in entries]


def message_media(message: Any, *, thread_id: str) -> list[dict[str, Any]]:
    """The :func:`media_items` of a checkpointed tool message; a message from before tool media was kept gets a
    ``not_saved`` placeholder per media block instead."""
    entries = (getattr(message, "additional_kwargs", None) or {}).get(DAIV_MEDIA_KEY)
    if entries is not None:
        return media_items(entries, thread_id=thread_id)
    return [_legacy_item(block) for block in media_blocks(message)]


def _item(entry: dict[str, Any], thread_id: str) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": entry.get("kind") or "file", "mime_type": entry.get("mime_type") or ""}
    if "size" in entry:
        item["size"] = entry["size"]
    if media_id := entry.get("id"):
        try:
            return {**item, "url": reverse("session_tool_media", kwargs={"thread_id": thread_id, "pk": media_id})}
        except NoReverseMatch:
            logger.warning("Tool media %r on thread %r has no URL", media_id, thread_id)
            return {**item, "error": FAILED}
    if url := entry.get("url"):
        return {**item, **_external(url)}
    return {**item, "error": entry.get("error") or FAILED}


def _legacy_item(block: dict[str, Any]) -> dict[str, Any]:
    item = {"kind": block["type"], "mime_type": block.get("mime_type") or ""}
    if url := block.get("url"):
        return {**item, **_external(url)}
    return {**item, "error": NOT_SAVED}


def _external(url: str) -> dict[str, Any]:
    if urlsplit(url).scheme in _LINK_SCHEMES:
        return {"url": url, "external": True}
    return {"error": UNSUPPORTED_TYPE}
