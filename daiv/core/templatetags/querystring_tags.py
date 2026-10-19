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
