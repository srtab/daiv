import re

from codebase.base import GitPlatform

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


def clean_job_logs(log: str, git_platform: GitPlatform, failed: bool = False) -> str:
    """
    Clean logs for failed jobs by removing irrelevant information and extracting the last command.

    Args:
        log: The logs to clean
        git_platform: The Git platform
        failed: Whether the job failed

    Returns:
        Cleaned logs
    """
    if git_platform == GitPlatform.GITLAB:
        cleaned = _clean_ansi_codes(replace_section_start_and_end_markers(log))
        return extract_last_command_from_gitlab_logs(cleaned) if failed else cleaned
    elif git_platform == GitPlatform.GITHUB:
        cleaned = strip_iso_timestamps(_clean_ansi_codes(log))
        return extract_last_command_from_github_logs(cleaned) if failed else cleaned
    return log


def safe_slug(name: str) -> str:
    """
    Create a safe slug from a string.

    Args:
        name: The string to create a safe slug from

    Returns:
        A safe slug
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
