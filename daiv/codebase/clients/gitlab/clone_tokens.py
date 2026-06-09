from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from django.core.cache import cache

from gitlab.exceptions import GitlabError

if TYPE_CHECKING:
    from gitlab import Gitlab

logger = logging.getLogger("daiv.clients")

CLONE_TOKEN_NAME = "daiv-clone"  # noqa: S105
CLONE_TOKEN_SCOPES = ["write_repository"]
CLONE_TOKEN_ACCESS_LEVEL = 30  # Developer: enough to push non-protected branches.
# Expiry is date-granular (token dies at 00:00 UTC on expires_at). Three days guarantees a
# token served at the very end of the 24h cache window still has >=24h of validity left.
CLONE_TOKEN_LIFETIME_DAYS = 3
CLONE_TOKEN_CACHE_TIMEOUT = 60 * 60 * 24
CLONE_TOKEN_UNAVAILABLE_TIMEOUT = 60 * 60

_CACHE_KEY = "codebase:gitlab:clone-token:{project_pk}"
_UNAVAILABLE = "__unavailable__"


def get_ephemeral_clone_token(client: Gitlab, project_pk: int) -> str | None:
    """
    Get a short-lived project access token suitable for git clone/push over HTTPS.

    The token is scoped to the single project (``write_repository``) so the credential
    embedded in the clone's ``.git/config`` — which travels into the sandbox — cannot
    reach the API or other projects. Tokens are minted at most once per day per project
    and retired by natural expiry only: rotating or revoking would instantly invalidate
    the token inside still-running jobs that cloned with it.

    Returns ``None`` when the token cannot be provisioned (e.g. the PAT user lacks the
    Maintainer role, or the instance tier does not support project access tokens); the
    failure is cached for one hour to avoid hammering the API on every clone.
    """
    cache_key = _CACHE_KEY.format(project_pk=project_pk)
    if (cached := cache.get(cache_key)) is not None:
        return None if cached == _UNAVAILABLE else cached

    # Deliberately not locked: if two workers miss the cache simultaneously, each mints its own
    # token — both are valid, the extra one just expires naturally. That happens at most ~once a
    # day per project and is cheaper than coordinating across workers.
    token = _create_token(client, project_pk)
    if token is None:
        cache.set(cache_key, _UNAVAILABLE, CLONE_TOKEN_UNAVAILABLE_TIMEOUT)
    else:
        cache.set(cache_key, token, CLONE_TOKEN_CACHE_TIMEOUT)
    return token


def _create_token(client: Gitlab, project_pk: int) -> str | None:
    expires_at = (datetime.now(UTC).date() + timedelta(days=CLONE_TOKEN_LIFETIME_DAYS)).isoformat()
    project = client.projects.get(project_pk, lazy=True)
    try:
        token = project.access_tokens.create({
            "name": CLONE_TOKEN_NAME,
            "scopes": CLONE_TOKEN_SCOPES,
            "access_level": CLONE_TOKEN_ACCESS_LEVEL,
            "expires_at": expires_at,
        })
    except GitlabError as e:
        logger.warning(
            "Could not create an ephemeral clone token for project %s; falling back to the PAT: %s", project_pk, e
        )
        return None
    return token.token
