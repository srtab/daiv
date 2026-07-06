import base64
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from pydantic import SecretStr

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


@dataclass(frozen=True)
class GitAuthEnv:
    """A credential + prompt-disabling environment overlay for git-over-HTTPS, carried without the
    token ever touching argv (``ps``-visible) or ``.git/config`` (which is seeded into the sandbox).

    The credential is held in :class:`~pydantic.SecretStr` — like the sibling
    :class:`codebase.clients.base.GitEgressCredential` — so it never appears verbatim in a ``repr``,
    a log line, or a Sentry stack-local; the plaintext is materialised only by :meth:`as_env`, which
    callers invoke at the innermost subprocess/clone boundary. Build via :meth:`for_token`.
    """

    config_key: str
    """The ``http.<origin>.extraheader`` config key. Keeps the clone URL's scheme and port because
    git matches ``http.<url>.*`` by prefix: a mismatch would silently send no credential."""

    header: SecretStr
    """The ``Authorization: Basic base64("oauth2:<token>")`` header value — the same shape the egress
    proxy injects (see :meth:`codebase.clients.base.GitEgressCredential.for_token`)."""

    @classmethod
    def for_token(cls, clone_url: str, token: str) -> GitAuthEnv:
        """Build the overlay for ``clone_url``'s origin authenticated with ``token``.

        Args:
            clone_url: The repository's credential-less HTTP(S) clone URL.
            token: The token to authenticate with.
        """
        parsed = urlparse(clone_url)
        encoded = base64.b64encode(f"oauth2:{token}".encode()).decode()
        return cls(
            config_key=f"http.{parsed.scheme}://{parsed.netloc}/.extraheader",
            header=SecretStr(f"Authorization: Basic {encoded}"),
        )

    def as_env(self) -> dict[str, str]:
        """Materialise the overlay as git subprocess environment variables (plaintext credential).

        ``GIT_CONFIG_{COUNT,KEY_0,VALUE_0}`` apply the command-scoped ``extraheader``.
        ``GIT_TERMINAL_PROMPT=0`` **and** ``GIT_ASKPASS=""`` together disable every prompt path so a
        *rejected* credential fails fast with ``could not read Username`` — one of
        :func:`core.utils.is_git_auth_error_text`'s markers, so the clone-retry self-heal and
        push-failure classifier keep recognising auth errors. Both are needed: with only
        ``GIT_TERMINAL_PROMPT=0`` git still falls back to an inherited ``SSH_ASKPASS`` GUI helper and
        hangs; the empty ``GIT_ASKPASS`` is non-null (short-circuiting that fallback chain) yet empty
        (so nothing is executed), leaving only the disabled terminal prompt.
        """
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": self.config_key,
            "GIT_CONFIG_VALUE_0": self.header.get_secret_value(),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
        }


def safe_slug(name: str) -> str:
    """
    Create a safe slug from a string.

    Args:
        name: The string to create a safe slug from

    Returns:
        A safe slug
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
