from __future__ import annotations

import re

from django import template
from django.templatetags.static import static
from django.utils.html import conditional_escape
from django.utils.safestring import SafeString, mark_safe

register = template.Library()

_VALID_NAME = re.compile(r"\A[a-z0-9][a-z0-9_-]*\Z", re.IGNORECASE)


@register.simple_tag
def icon(name: str, css_class: str = "") -> SafeString:
    """Return a `<span>` masked by `core/img/icons/{name}.svg`, painted with `currentColor`.

    `name` must match `[A-Za-z0-9][A-Za-z0-9_-]*` — it is interpolated unescaped
    into a `style="url(...)"` attribute, so callers must keep it a trusted slug.
    `css_class` is HTML-escaped before insertion.
    """
    if not _VALID_NAME.match(name):
        raise ValueError(f"icon name must be a filename slug, got {name!r}")
    url = static(f"core/img/icons/{name}.svg")
    css = conditional_escape(css_class)
    return mark_safe(  # noqa: S308 — name is validated above; url is its static() output; css is HTML-escaped.
        f'<span class="inline-block {css}" style="background-color: currentColor; '
        f"-webkit-mask: url('{url}') no-repeat center / contain; "
        f"mask: url('{url}') no-repeat center / contain;\"></span>"
    )
