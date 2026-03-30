from django import template

register = template.Library()


@register.filter
def success_rate_color(value):
    """Return a Tailwind text color class based on a success-rate percentage.

    Accepts a string like "85%" and returns a color reflecting the rate.
    Non-percentage values (integers, "—", etc.) fall through to "text-white".
    """
    try:
        pct = int(value.rstrip("%"))
    except ValueError, AttributeError:
        return "text-white"

    if pct >= 90:
        return "text-emerald-400"
    if pct >= 75:
        return "text-amber-400"
    return "text-red-400"
