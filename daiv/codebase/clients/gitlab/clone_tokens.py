from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from django.core.cache import cache

import requests
from gitlab.exceptions import GitlabAuthenticationError, GitlabError

if TYPE_CHECKING:
    from gitlab import Gitlab

logger = logging.getLogger("daiv.clients")

CLONE_TOKEN_NAME = "daiv-clone"  # noqa: S105
CLONE_TOKEN_SCOPES = ["write_repository"]
CLONE_TOKEN_ACCESS_LEVEL = 30  # Developer: enough to push non-protected branches.
# Expiry is date-granular (token dies at 00:00 UTC on expires_at). Three days guarantees a token
# served at the very end of the CLONE_TOKEN_CACHE_TIMEOUT window still has >=24h of validity left.
CLONE_TOKEN_LIFETIME_DAYS = 3
CLONE_TOKEN_CACHE_TIMEOUT = 60 * 60 * 24
# Persistent failures (role, tier, dead PAT): retrying soon won't change the answer.
CLONE_TOKEN_UNAVAILABLE_TIMEOUT = 60 * 60
# Transient failures (network blips, 429, 5xx): retry soon so a single blip doesn't park
# clones on the PAT for a whole hour.
CLONE_TOKEN_TRANSIENT_UNAVAILABLE_TIMEOUT = 60 * 5

_CACHE_KEY = "codebase:gitlab:clone-token:{project_pk}"
_UNAVAILABLE = "__unavailable__"


def get_ephemeral_clone_token(client: Gitlab, project_pk: int) -> str | None:
    """
    Get a short-lived project access token suitable for git clone/push over HTTPS.

    The token is scoped to the single project (``write_repository``) so the credential
    embedded in the clone's ``.git/config`` — which travels into the sandbox — cannot
    reach the API or other projects. Tokens are minted roughly once per day per project
    (per shared cache) and retired by natural expiry only: rotating or revoking would
    instantly invalidate the token inside still-running jobs that cloned with it.

    Returns ``None`` when the token cannot be provisioned (e.g. the PAT user lacks the
    Maintainer role, the instance tier does not support project access tokens, or a
    transient API/network error); the failure is negative-cached — for an hour when it
    looks persistent, minutes when transient — to avoid hammering the API on every clone.
    """
    cache_key = _CACHE_KEY.format(project_pk=project_pk)
    if (cached := cache.get(cache_key)) is not None:
        return None if cached == _UNAVAILABLE else cached

    # Deliberately not locked: if two workers miss the cache simultaneously, each mints its own
    # token — both are valid, the extra one just expires naturally. That happens at most ~once a
    # day per project and is cheaper than coordinating across workers.
    token, cache_timeout = _create_token(client, project_pk)
    cache.set(cache_key, token or _UNAVAILABLE, cache_timeout)
    return token


def invalidate_clone_token(project_pk: int) -> None:
    """
    Evict the cached clone-token outcome for a project so the next call re-mints.

    Used when a clone authenticates with the cached token yet GitLab rejects it (revoked,
    expired, or the project/instance was reset): without eviction the dead token would be
    served for the remainder of the cache window, failing every clone of the project. A
    no-op when nothing is cached.
    """
    cache.delete(_CACHE_KEY.format(project_pk=project_pk))


def _create_token(client: Gitlab, project_pk: int) -> tuple[str | None, int]:
    """
    Mint the project access token.

    Returns ``(token, cache_timeout)``: the token secret (``None`` when provisioning failed)
    and how long that outcome should be cached — a day for a minted token, an hour for
    persistent failures (role, tier, dead PAT), minutes for transient ones (network, 429, 5xx).
    """
    expires_at = (datetime.now(UTC).date() + timedelta(days=CLONE_TOKEN_LIFETIME_DAYS)).isoformat()
    project = client.projects.get(project_pk, lazy=True)
    try:
        token = project.access_tokens.create({
            "name": CLONE_TOKEN_NAME,
            "scopes": CLONE_TOKEN_SCOPES,
            "access_level": CLONE_TOKEN_ACCESS_LEVEL,
            "expires_at": expires_at,
        })
    except GitlabAuthenticationError as e:
        # The fallback embeds the very credential GitLab just rejected, so the clone is likely
        # to fail too — name the real culprit instead of claiming a benign degradation.
        logger.warning(
            "GitLab rejected the configured PAT while provisioning a clone token for project %s "
            "(is the PAT revoked or expired?); the clone will fall back to the PAT: %s",
            project_pk,
            e,
        )
        return None, CLONE_TOKEN_UNAVAILABLE_TIMEOUT
    except (GitlabError, requests.RequestException) as e:
        logger.warning(
            "Could not create an ephemeral clone token for project %s (the clone will fall back to the PAT): %s",
            project_pk,
            e,
        )
        return None, CLONE_TOKEN_TRANSIENT_UNAVAILABLE_TIMEOUT if _is_transient(e) else CLONE_TOKEN_UNAVAILABLE_TIMEOUT
    if not (secret := getattr(token, "token", None)):
        # The create call succeeded, so GitLab now holds a token it never disclosed to us.
        logger.warning(
            "GitLab returned an access token for project %s without a token secret; the clone will fall "
            "back to the PAT (an orphaned '%s' token was likely left on the project).",
            project_pk,
            CLONE_TOKEN_NAME,
        )
        return None, CLONE_TOKEN_UNAVAILABLE_TIMEOUT
    return secret, CLONE_TOKEN_CACHE_TIMEOUT


def _is_transient(e: GitlabError | requests.RequestException) -> bool:
    if isinstance(e, requests.RequestException):
        return True
    return e.response_code == 429 or (e.response_code or 0) >= 500  # noqa: PLR2004
