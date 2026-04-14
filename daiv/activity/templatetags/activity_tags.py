from decimal import Decimal

from django import template

register = template.Library()

_CENT = Decimal("0.01")


@register.filter
def duration(value):
    """Format a duration in seconds as a compact human-readable string."""
    if value is None:
        return ""
    total_seconds = int(value)
    if total_seconds < 0:
        return ""
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


@register.filter
def format_cost(value):
    """Format a Decimal cost as a compact USD string."""
    if value is None:
        return ""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if d < _CENT:
        return f"${d:.4f}"
    return f"${d:.2f}"


@register.filter
def format_tokens(value):
    """Format token count with compact suffixes (1.2k, 45.3k, 1.2M)."""
    if value is None:
        return ""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)
