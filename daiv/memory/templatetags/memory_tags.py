from django import template

register = template.Library()


@register.filter
def query_param(key: str, value: str | None = None) -> dict[str, str | None]:
    """Return ``{key: value}`` for ``{% querystring %}``, whose keyword names can't come from a variable."""
    return {key: value}
