from django import template

register = template.Library()


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
