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


def extract_last_command_from_github_logs(log: str) -> str:
    """
    Clean GitHub logs by removing irrelevant information and extracting the last failed command.

    Args:
        log: Full log containing multiple commands and outputs

    Returns:
        Output of the last executed command or an empty string if no command was found
    """
    # Find the failure line (non-zero exit)
    m_fail = re.search(r"##\[error\]Process completed with exit code (\d+)\.", log)

    if not m_fail or int(m_fail.group(1)) == 0:
        return ""

    fail_start = m_fail.start()

    # Last "##[group]Run ..." before the failure
    groups = list(re.finditer(r"##\[group\]Run .+", log))

    start_matches = [g for g in groups if g.start() < fail_start]
    if not start_matches:
        return ""

    start = start_matches[-1].end() + 1  # +1 to skip the newline

    # Capture until next group header or "Post job cleanup" or end
    next_boundary = re.search(r"(?:##\[group\]|Post job cleanup)", log[start + 1 :])
    end = (start + 1 + next_boundary.start()) if next_boundary else len(log)

    return log[start:end].rstrip()


def get_github_cli_token() -> str:
    """
    Get the GitHub CLI token for the current installation.
    """
    base_url = Consts.DEFAULT_BASE_URL
    if settings.GITHUB_URL:
        base_url = str(settings.GITHUB_URL)

    integration = GithubIntegration(
        auth=Auth.AppAuth(settings.GITHUB_APP_ID, settings.GITHUB_PRIVATE_KEY.get_secret_value()),
        base_url=base_url,
        user_agent=USER_AGENT,
    )
    return integration.get_access_token(settings.GITHUB_INSTALLATION_ID).token
