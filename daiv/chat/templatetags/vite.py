from __future__ import annotations

import json
import logging
from pathlib import Path

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.utils.html import format_html, format_html_join

logger = logging.getLogger(__name__)
register = template.Library()


def _manifest_path() -> Path:
    found = finders.find("chat/dist/manifest.json")
    if found:
        return Path(found)
    if settings.STATIC_ROOT:
        candidate = Path(settings.STATIC_ROOT) / "chat" / "dist" / "manifest.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("vite manifest not found via static finders or STATIC_ROOT")


@register.simple_tag
def vite_asset(entry: str) -> str:
    dev_server = getattr(settings, "VITE_DEV_SERVER", None)
    if dev_server:
        return format_html(
            '<script type="module" src="{}/@vite/client"></script><script type="module" src="{}/{}"></script>',
            dev_server,
            dev_server,
            entry,
        )
    try:
        manifest_path = _manifest_path()
        manifest = json.loads(manifest_path.read_text())
        chunk = manifest[entry]
    except FileNotFoundError, json.JSONDecodeError, KeyError, OSError:
        logger.exception("vite_asset: failed to resolve %r from manifest", entry)
        return format_html("<!-- vite_asset: failed to load entry {} -->", entry)
    script = format_html('<script type="module" src="/static/chat/dist/{}"></script>', chunk["file"])
    css_links = format_html_join(
        "\n", '<link rel="stylesheet" href="/static/chat/dist/{}">', ((href,) for href in chunk.get("css", []))
    )
    if css_links:
        return format_html("{}\n{}", script, css_links)
    return script
