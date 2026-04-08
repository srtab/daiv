from __future__ import annotations

from django import template

register = template.Library()


@register.inclusion_tag("core/icons/_icon.html")
def icon(name: str, css_class: str = "") -> dict[str, str]:
    """Render an SVG icon from the static icons directory using a CSS mask."""
    return {"icon_path": f"core/img/icons/{name}.svg", "css_class": css_class}
