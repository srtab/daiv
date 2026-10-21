import re

from github import Auth, Consts, GithubIntegration

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


def get_github_integration() -> GithubIntegration:
    """
    Get the GitHub integration for the current installation.
    """
    base_url = Consts.DEFAULT_BASE_URL
    if settings.GITHUB_URL:
        base_url = str(settings.GITHUB_URL)

    return GithubIntegration(
        auth=Auth.AppAuth(settings.GITHUB_APP_ID, settings.GITHUB_PRIVATE_KEY.get_secret_value()),
        base_url=base_url,
        user_agent=USER_AGENT,
    )
