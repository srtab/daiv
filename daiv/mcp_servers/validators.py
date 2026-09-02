from __future__ import annotations

import ipaddress
import socket
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


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    """Whether an IP literal is in a non-routable range (private, loopback,
    link-local, reserved, unspecified, multicast). Cloud-metadata endpoints
    (169.254.169.254) land here via ``is_link_local``."""
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified or ip.is_multicast


def _is_private_ip_str(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return _is_private_ip(ip)


def _resolve_host_ips(host: str) -> list[str]:
    """Best-effort DNS A/AAAA resolution of ``host`` to literal IP strings.

    Returns ``[]`` on any resolution failure (NXDOMAIN, timeout, no DNS) so callers
    treat "unresolvable" as "not confirmed internal" — reachability still fails
    loudly at connection time, per ``validate_http_url``. Module-level (not inline)
    so tests can monkeypatch the resolver without touching real DNS.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return [info[4][0] for info in infos]


def is_internal_network_target(url: str) -> bool:
    """Whether ``url`` points at a private/loopback/link-local network target.

    True when the host is an IP literal in a private/reserved/loopback/link-local
    range, the ``localhost`` name, or a hostname that resolves to such an IP.
    Unresolvable hostnames return False — reachability fails loudly at connection
    time, per ``validate_http_url``.

    This is the SSRF guard that gates member-controlled MCP servers: a personal
    (user-scoped) server or a non-admin "Test connection" probe must not point at
    the app host's internal network. Admin/global-configured rows bypass it —
    internal MCP servers are a legitimate deployment shape (cf.
    ``validate_http_url``'s docstring).
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = parts.hostname
    if not host:
        return False
    # ``localhost`` is loopback by definition and does not need DNS — pinning it
    # keeps the check deterministic when tests (or a sandbox) have no resolver.
    if host.lower() == "localhost":
        return True
    # urlsplit().hostname returns IPv6 literals bracket-stripped and IPv4 as the
    # bare string; ip_address parses both.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return any(_is_private_ip_str(ip_str) for ip_str in _resolve_host_ips(host))
    return _is_private_ip(ip)
