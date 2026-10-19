from django import template

register = template.Library()


@register.filter
def success_rate_color(value):
    """Return a Tailwind text color class based on a success-rate percentage.

    Accepts an integer (e.g. 85) or a string like "85%".
    Returns "text-white" for None or values that cannot be parsed as a number.
    """
    if value is None:
        return "text-white"
    try:
        pct = int(value) if isinstance(value, int) else int(str(value).rstrip("%"))
    except ValueError:
        return "text-white"

    if pct >= 90:
        return "text-emerald-400"
    if pct >= 75:
        return "text-amber-400"
    return "text-red-400"
