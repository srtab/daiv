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
