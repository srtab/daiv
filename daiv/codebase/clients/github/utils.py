import re
import threading
from typing import Any

from github import Auth, Consts, GithubIntegration
from github.Requester import HTTPRequestsConnectionClass, HTTPSRequestsConnectionClass, Requester

from codebase.conf import settings
from daiv import USER_AGENT

# Matches:
# 2025-09-22T21:40:35Z
# 2025-09-22T21:40:35.4116534Z
# 2025-09-22T21:40:35+01:00
# 2025-09-22T21:40:35.4116534+01:00
# Also supports lines prefixed by tab-separated columns, e.g.:
# Analyze (actions)    UNKNOWN STEP    2026-01-31T01:00:49.2161896Z <message>
_TS_PREFIX = re.compile(
    r"(?m)^(?:[^\n]*\t+)?\s*\ufeff?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\s+"
)


def strip_iso_timestamps(text: str) -> str:
    """
    Remove the datetime prefix at the start of each log line.

    This also supports "step logs" that include tab-separated prefixes before the timestamp
    (e.g. GitHub UI "Analyze (actions)\tUNKNOWN STEP\t<timestamp> ...").
    """
    return _TS_PREFIX.sub("", text)


def _per_thread(name: str) -> property:
    return property(lambda self: getattr(self._pending, name), lambda self, value: setattr(self._pending, name, value))


class _PerThreadPendingRequest:
    """PyGithub's connections hold a request on the connection between ``request()`` and ``getresponse()``, and every
    thread using the client shares that connection; held per thread, concurrent calls no longer send each other's
    request, while the connection's one ``requests.Session`` still pools sockets across threads."""

    verb = _per_thread("verb")
    url = _per_thread("url")
    input = _per_thread("input")
    headers = _per_thread("headers")
    stream = _per_thread("stream")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._pending = threading.local()
        super().__init__(*args, **kwargs)


class PerThreadHTTPConnection(_PerThreadPendingRequest, HTTPRequestsConnectionClass):
    pass


class PerThreadHTTPSConnection(_PerThreadPendingRequest, HTTPSRequestsConnectionClass):
    pass


def use_per_thread_connections() -> None:
    """Have every PyGithub ``Requester`` built from now on connect through the per-thread classes.

    Sets the class attributes ``Requester.injectConnectionClasses`` sets, without its switch to a new connection for
    every request.
    """
    for name, connection_class in (
        ("_Requester__httpConnectionClass", PerThreadHTTPConnection),
        ("_Requester__httpsConnectionClass", PerThreadHTTPSConnection),
    ):
        if not hasattr(Requester, name):
            raise RuntimeError(f"PyGithub's Requester no longer has {name}: re-check how it holds a request")
        setattr(Requester, name, connection_class)


def get_github_integration() -> GithubIntegration:
    """
    Get the GitHub integration for the current installation.
    """
    use_per_thread_connections()
    base_url = Consts.DEFAULT_BASE_URL
    if settings.GITHUB_URL:
        base_url = str(settings.GITHUB_URL)

    return GithubIntegration(
        auth=Auth.AppAuth(settings.GITHUB_APP_ID, settings.GITHUB_PRIVATE_KEY.get_secret_value()),
        base_url=base_url,
        user_agent=USER_AGENT,
    )
