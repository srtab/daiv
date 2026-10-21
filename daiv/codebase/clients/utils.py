import re

from codebase.base import GitPlatform

from .github.utils import strip_iso_timestamps
from .gitlab.utils import replace_section_start_and_end_markers


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


def clean_job_logs(log: str, git_platform: GitPlatform) -> str:
    """
    Clean job logs by removing ANSI codes and platform noise (GitLab section markers, GitHub timestamps).

    Args:
        log: The logs to clean
        git_platform: The Git platform

    Returns:
        Cleaned logs
    """
    if git_platform == GitPlatform.GITLAB:
        return _clean_ansi_codes(replace_section_start_and_end_markers(log))
    elif git_platform == GitPlatform.GITHUB:
        return strip_iso_timestamps(_clean_ansi_codes(log))
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
