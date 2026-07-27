import re
from contextlib import contextmanager
from typing import TYPE_CHECKING

from git import GitCommandError

from codebase.base import GitPlatform
from codebase.exceptions import RepositoryRefNotFoundError

from .github.utils import extract_last_command_from_github_logs, strip_iso_timestamps
from .gitlab.utils import extract_last_command_from_gitlab_logs, replace_section_start_and_end_markers

if TYPE_CHECKING:
    from collections.abc import Iterator

    from codebase.clients.base import RepoClient


_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{7,40}")


def _is_missing_ref_error(error: GitCommandError) -> bool:
    """True when a clone failed because the requested ref was absent from the remote's advertisement.

    ``git clone --branch=<ref>`` reports the miss as ``fatal: Remote branch <ref> not found in
    upstream origin``, sometimes preceded by ``warning: Could not find remote branch <ref> to
    clone.`` — match both wordings, and neither the ref name nor the remote name, so the check
    survives git's phrasing differences across versions and transports. Only ``stderr`` is
    searched: ``str(error)`` also carries the cmdline (which embeds ``--branch=<ref>``) and
    stdout, and widening the haystack buys nothing these two markers don't already cover.
    """
    lowered = (error.stderr or "").lower()
    return "not found in upstream" in lowered or "could not find remote branch" in lowered


@contextmanager
def translate_missing_ref(client: RepoClient, repo_id: str, ref: str) -> Iterator[None]:
    """Re-raise a *confirmed* missing-branch clone failure as :class:`RepositoryRefNotFoundError`.

    Wraps the clone in both platform clients so a vanished ref reaches callers as an actionable,
    recoverable condition (retarget the default branch) rather than an opaque git failure. Every
    other ``GitCommandError`` — notably auth rejections, which have their own propagation-retry
    and token self-heal path in the GitLab client — propagates untouched.

    git's wording is necessary but **not sufficient** evidence: it says the ref was absent from
    the advertisement this credential received, which is also what a replication lag, a
    restricted ref view, or a stale proxy looks like. This instance is already known to report a
    just-pushed branch as missing for a window (see
    ``MERGE_REQUEST_BRANCH_VISIBILITY_RETRY_BACKOFF_SECONDS``), and acting on a false positive is
    destructive — the chat fallback rewrites the session's stored ref. So the platform API must
    also say the branch is gone; an unknown answer (``None``, i.e. the API could not be reached)
    keeps the raw git error.

    A SHA-shaped ref is excluded too: ``--branch`` resolves branches and tags but never a commit
    SHA, so the same message there means "that isn't a branch", not "it was deleted" — and
    callers may legitimately pin a SHA (the MCP ``submit_job`` contract allows one).
    """
    try:
        yield
    except GitCommandError as e:
        if (
            _is_missing_ref_error(e)
            and _COMMIT_SHA_RE.fullmatch(ref) is None
            and client.branch_exists(repo_id, ref) is False
        ):
            raise RepositoryRefNotFoundError(repo_id, ref) from e
        raise


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
