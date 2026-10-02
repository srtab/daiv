from __future__ import annotations

from typing import Any

from django import template

register = template.Library()


@register.filter(name="get_field")
def get_field(form: Any, field_name: str) -> Any:
    """Get a form field by name."""
    try:
        return form[field_name]
    except KeyError:
        return None
