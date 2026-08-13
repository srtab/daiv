from __future__ import annotations

from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

_ALLOWED_SCHEMES = ("http", "https")


def validate_http_url(value: str) -> None:
    """Validate an absolute http(s) URL while permitting internal hostnames.

    Django's ``URLValidator`` rejects single-label hosts other than ``localhost``
    (e.g. ``mcpserver``) and any host containing an underscore (e.g. Docker Compose
    service names such as ``mcp_rt``). Those are unreachable on the public internet
    but perfectly valid on an internal network — the usual place an MCP server lives
    in this deployment (cf. the ``SANDBOX_URL`` default of ``http://sandbox:8000``).
    So we only require a well-formed absolute http(s) URL with a host, and leave
    reachability to fail loudly at connection time.
    """
    error = ValidationError(_("Enter a valid http(s) URL, e.g. http://mcp-server:8000/mcp."), code="invalid")
    # urlsplit() silently strips embedded tab/newline/CR before parsing, but the form
    # only strips *surrounding* whitespace — so a value with embedded whitespace would
    # validate against a sanitized copy yet be stored verbatim. Reject it up front so
    # what we validate is exactly what gets persisted.
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        raise error
    try:
        parts = urlsplit(value)
        _port = parts.port  # property access raises ValueError on a non-numeric / out-of-range port
    except ValueError as exc:
        raise error from exc
    if parts.scheme not in _ALLOWED_SCHEMES or not parts.hostname:
        raise error
