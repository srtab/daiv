from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def querystring_without(context, *exclude_keys):
    """Build a querystring from the current request, excluding specified keys.

    Returns a string like "q=foo&role=admin&" (with trailing ampersand) ready to
    be followed by additional parameters, or an empty string if no params remain.
    """
    request = context["request"]
    params = request.GET.copy()
    for key in exclude_keys:
        params.pop(key, None)
    encoded = params.urlencode()
    return f"{encoded}&" if encoded else ""


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
