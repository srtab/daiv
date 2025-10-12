import re

from codebase.base import ClientType

from .github.utils import extract_last_command_from_github_logs, strip_iso_timestamps
from .gitlab.utils import extract_last_command_from_gitlab_logs, replace_section_start_and_end_markers


def _clean_ansi_codes(log: str) -> str:
    """
    Remove ANSI escape codes from logs.

    Args:
        log: Raw log content with ANSI codes

    Returns:
        Cleaned log content
    """
    # Replace Windows line endings with Unix line endings
    content = log.replace("\r\n", "\n")
    # Replace carriage return with newline
    content = content.replace("\r", "\n")

    # Remove ANSI escape codes
    content = re.sub(r"\x1B\[[0-9;]*[a-zA-Z]", "", content)

    return content


def clean_job_logs(log: str, client_type: ClientType, failed: bool = False) -> str:
    """
    Clean logs for failed jobs by removing irrelevant information and extracting the last command.

    Args:
        log: The logs to clean
        client_type: The client type (GitLab or GitHub)
        failed: Whether the job failed

    Returns:
        Cleaned logs
    """
    if client_type == ClientType.GITLAB:
        cleaned = _clean_ansi_codes(replace_section_start_and_end_markers(log))
        return extract_last_command_from_gitlab_logs(cleaned) if failed else cleaned
    elif client_type == ClientType.GITHUB:
        cleaned = strip_iso_timestamps(_clean_ansi_codes(log))
        return extract_last_command_from_github_logs(cleaned) if failed else cleaned
    return log
