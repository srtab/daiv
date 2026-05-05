from __future__ import annotations

import json
from pathlib import Path

from django import template
from django.conf import settings
from django.utils.safestring import mark_safe

register = template.Library()


def _manifest_path() -> Path:
    for static_dir in settings.STATICFILES_DIRS:
        candidate = Path(static_dir) / "chat" / "dist" / "manifest.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("vite manifest not found in any STATICFILES_DIRS")


@register.simple_tag
def vite_asset(entry: str) -> str:
    dev_server = getattr(settings, "VITE_DEV_SERVER", None)
    if dev_server:
        return mark_safe(  # noqa: S308
            f'<script type="module" src="{dev_server}/@vite/client"></script>'
            f'<script type="module" src="{dev_server}/{entry}"></script>'
        )
    manifest = json.loads(_manifest_path().read_text())
    chunk = manifest[entry]
    tags = [f'<script type="module" src="/static/chat/dist/{chunk["file"]}"></script>']
    for css in chunk.get("css", []):
        tags.append(f'<link rel="stylesheet" href="/static/chat/dist/{css}">')
    return mark_safe("\n".join(tags))  # noqa: S308
