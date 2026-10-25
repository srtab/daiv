"""Resolution, refresh and revocation of per-user git platform OAuth credentials.

The only module permitted to decrypt :attr:`accounts.models.PlatformCredential.access_token`.
Every other caller asks here and receives either a usable token or a typed
:class:`CredentialReason`.

Five invariants this module exists to keep:

* a refresh writes access token, refresh token and expiry in **one transaction**, because GitLab
  rotates the refresh token on every use;
* refresh happens at the point of use, within :data:`REFRESH_MARGIN_SECONDS` of expiry, not once
  per run — a long run outlives a 2-hour GitLab token;
* clearing a grant is **irreversible**, so only the platform naming it dead does it. A transport
  error, a 5xx or a rotated-token race yields :attr:`CredentialReason.REFRESH_FAILED` and leaves
  the row alone. A grant DAIV gives up on logs at ``error`` — an authorisation lost to key
  rotation or a dead refresh token is operator-actionable; a person revoking their own is not;
* no token, and no fragment of one, reaches a log record or a ``repr``;
* a local holding a secret is named ``access_token``, ``refresh_token``, ``token`` or ``client_secret``,
  because Sentry's scrubber matches frame-local names exactly.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

import httpx
from asgiref.sync import sync_to_async

from codebase.base import GitPlatform
from codebase.conf import settings as codebase_settings
from core.encryption import DecryptionError
from core.site_settings import site_settings
from daiv import USER_AGENT

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterable, Sequence

logger = logging.getLogger("daiv.accounts")


REFRESH_MARGIN_SECONDS = 300
"""Renew a token that expires within this window. Wider than one CLI call's 30-second timeout
plus retries, narrow enough not to refresh on every call."""

TOKEN_CACHE_PREFIX = "platform_credential_token"  # noqa: S105
REFRESH_TIMEOUT_SECONDS = 15
REFRESH_LOCK_SECONDS = 30
"""How long one refresh may hold a grant's lock, and how long another waits for it. Above
:data:`REFRESH_TIMEOUT_SECONDS`, so a lock outlives the request it guards."""

REAUTHORISE_SESSION_KEY = "platform_credential_reauthorise"
"""Set by the Authorise button to the person's pk; the sign-in that completes the flow consumes it."""

GITLAB_CROSS_PROJECT_SCOPES = ("api", "read_api")
"""Either reads another project; only ``api`` can also write there, which GitLab enforces. A grant
holding neither is the older ``read_user``-only authorisation, refused so the person is told to re-authorise."""


class CredentialReason(StrEnum):
    """Why no token can be issued. One reason per cause — never collapsed into a generic failure,
    because the refusal the person reads has to name which thing is wrong."""

    DISABLED = "disabled"
    WEBHOOK_RUNS_DISABLED = "webhook_runs_disabled"
    NO_ACTING_USER = "no_acting_user"
    NO_CREDENTIAL = "no_credential"
    EXPIRED = "expired"
    REVOKED = "revoked"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    REFRESH_FAILED = "refresh_failed"
    UNREADABLE = "unreadable"
    REJECTED = "rejected"
    """The platform refused a token that carries no refresh token, so only the person can replace it."""


class RefreshFailure(StrEnum):
    """Whether a failed renewal condemns the grant or only this attempt.

    Only the platform refusing the grant itself is terminal. A transport error, a 5xx or a
    malformed body says nothing about the refresh token, and clearing it on those grounds is
    unrecoverable — the person must re-authorise for what was a momentary outage.
    """

    TERMINAL = "terminal"
    TRANSIENT = "transient"


TERMINAL_OAUTH_ERRORS = frozenset({"invalid_grant", "bad_refresh_token"})
"""The codes that unambiguously say "this refresh token is dead": RFC 6749's ``invalid_grant`` (GitLab) and
GitHub's ``bad_refresh_token``. Everything else — including ``invalid_request`` and ``server_error`` — can be
our bug or the platform's, so it is transient."""


@dataclass(frozen=True, repr=False)
class ResolvedCredential:
    """Either a usable token or the reason there is none — never both."""

    token: str | None = None
    reason: CredentialReason | None = None
    scopes: tuple[str, ...] = ()
    user_id: int | None = None

    def __post_init__(self) -> None:
        if (self.token is None) == (self.reason is None):
            raise ValueError("a resolved credential carries exactly one of a token or a reason")

    @property
    def ok(self) -> bool:
        return self.token is not None

    def __repr__(self) -> str:
        return f"ResolvedCredential(ok={self.ok}, reason={self.reason}, user_id={self.user_id})"


@dataclass(frozen=True)
class CredentialStatus:
    """What the account-settings page may show. Carries no secret."""

    connected: bool
    state: str | None = None
    host: str | None = None
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()
    permits_cross_project: bool = False


OAUTH_CAPABLE_PLATFORMS = (GitPlatform.GITLAB, GitPlatform.GITHUB)


def platform_host(provider: GitPlatform | str) -> str:
    """The platform origin this deployment's credentials are valid for."""
    provider = GitPlatform(provider)
    if provider == GitPlatform.GITLAB:
        url = codebase_settings.GITLAB_URL
        return url.host if url is not None and url.host else "gitlab.com"
    if provider == GitPlatform.GITHUB:
        url = codebase_settings.GITHUB_URL
        return url.host if url is not None and url.host else "github.com"
    raise ValueError(f"{provider} issues no per-user OAuth credential.")


def _gitlab_auth_base() -> str:
    """The GitLab origin allauth exchanges and refreshes tokens against: the server-side route when one is set."""
    return str(site_settings.auth_gitlab_server_url or site_settings.auth_gitlab_url or "")


def _hostname(url: Any) -> str | None:
    url = str(url or "")
    if not url:
        return None
    return urlparse(url if "://" in url else f"https://{url}").hostname


def auth_hosts(provider: GitPlatform | str) -> frozenset[str]:
    """The hosts the OAuth grant is minted by, empty for GitHub or when sign-in names no GitLab.

    ``platform_host`` derives from the *codebase* URL while allauth mints the token against
    ``auth_gitlab_url``. Nothing else compares them, so a deployment that sets one and not the
    other would send a gitlab.com token to its internal host. ``auth_gitlab_server_url`` is the same
    GitLab reached over an internal route, so the codebase URL may name either.
    """
    if GitPlatform(provider) != GitPlatform.GITLAB:
        return frozenset()
    names = (_hostname(site_settings.auth_gitlab_url), _hostname(site_settings.auth_gitlab_server_url))
    return frozenset(name for name in names if name)


def scopes_permit_cross_project(provider: GitPlatform | str, scopes: Sequence[str]) -> bool:
    """Whether a recorded grant is wide enough to reach another project.

    A GitHub App ignores the OAuth ``scope`` parameter entirely — reach is the intersection of the
    person's permissions and the App's installed ones, so there is nothing here to check.

    An empty list means the platform did not report what it granted, not that it granted nothing:
    the platform's own check is the real enforcement point, so an unknown grant is attempted
    rather than pre-emptively refused.
    """
    if GitPlatform(provider) == GitPlatform.GITHUB:
        return True
    if not scopes:
        return True
    return any(scope in scopes for scope in GITLAB_CROSS_PROJECT_SCOPES)


async def aresolve_access_token(
    *,
    provider: GitPlatform | str,
    acting_user_id: int | None = None,
    platform_uid: str | int | None = None,
    host: str | None = None,
) -> ResolvedCredential:
    """Resolve the token of the person a run acts for, or the typed reason there is none.

    Args:
        provider: The deployment's git platform.
        acting_user_id: The DAIV sign-in a chat, job, MCP or schedule run acts for.
        platform_uid: The platform user that triggered a webhook run; never a username or email match.
        host: The platform host; this deployment's when omitted.
    """
    if acting_user_id is not None and platform_uid is not None:
        raise ValueError("name the acting person by acting_user_id or platform_uid, not both")
    if not await _acapability_enabled():
        return ResolvedCredential(reason=CredentialReason.DISABLED)
    if acting_user_id is None and platform_uid is None:
        return ResolvedCredential(reason=CredentialReason.NO_ACTING_USER)
    if platform_uid is not None and not await _awebhook_runs_enabled():
        return ResolvedCredential(reason=CredentialReason.WEBHOOK_RUNS_DISABLED)

    from accounts.models import PlatformCredential

    host = host or platform_host(provider)
    provider_value = GitPlatform(provider).value
    rows = PlatformCredential.objects.filter(provider=provider_value, host=host, user__is_active=True)
    if acting_user_id is not None:
        rows = rows.filter(user_id=acting_user_id)
    else:
        rows = rows.filter(platform_uid=str(platform_uid))
    candidates = [row async for row in rows[:2]]
    if len(candidates) > 1:
        logger.error("Two DAIV accounts hold a %s grant for uid=%s; refusing both.", provider_value, platform_uid)
        return ResolvedCredential(reason=CredentialReason.NO_CREDENTIAL)
    if not candidates:
        return ResolvedCredential(reason=CredentialReason.NO_CREDENTIAL)
    credential = candidates[0]
    owner_id = credential.user_id

    if (dead := _dead_reason(credential.state)) is not None:
        return ResolvedCredential(reason=dead, user_id=owner_id)

    scopes = _as_scopes(credential.scopes)
    if not scopes_permit_cross_project(provider, scopes):
        return ResolvedCredential(reason=CredentialReason.INSUFFICIENT_SCOPE, scopes=scopes, user_id=owner_id)

    if _needs_refresh(credential):
        refreshed = await _arefresh(credential)
        if isinstance(refreshed, CredentialReason):
            return ResolvedCredential(reason=refreshed, user_id=owner_id)
        credential = refreshed
        scopes = _as_scopes(credential.scopes)

    try:
        token = credential.access_token
    except DecryptionError:
        logger.error(
            "The %s grant for user_id=%s cannot be decrypted; expiring it. DAIV_ENCRYPTION_KEY may have rotated.",
            provider_value,
            owner_id,
        )
        await aexpire(user_id=owner_id, provider=provider_value, host=host)
        return ResolvedCredential(reason=CredentialReason.UNREADABLE, user_id=owner_id)

    if not token:
        return ResolvedCredential(reason=CredentialReason.EXPIRED, user_id=owner_id)

    return ResolvedCredential(token=token, scopes=scopes, user_id=owner_id)


def _rows_for(user_id: int, provider: GitPlatform | str, host: str | None):
    """Rows for one identity, preferring ``host`` but never limited to it when none was asked for.

    ``platform_host`` is derived live from settings while the row's host was frozen at sign-in, so
    a deployment that has since moved its platform URL would otherwise hide the person's own grant
    from the page that offers to disconnect it.
    """
    from accounts.models import PlatformCredential

    rows = PlatformCredential.objects.filter(user_id=user_id, provider=GitPlatform(provider).value)
    return rows.filter(host=host) if host is not None else rows.order_by("-modified")


def status(*, user_id: int, provider: GitPlatform | str, host: str | None = None) -> CredentialStatus:
    """State, expiry and granted scopes for the account-settings page. Never the secret."""
    from accounts.models import CredentialState

    credential = _rows_for(user_id, provider, host).first()
    if credential is None:
        return CredentialStatus(connected=False, host=host or platform_host(provider))
    return CredentialStatus(
        connected=credential.state == CredentialState.CONNECTED,
        state=credential.state,
        host=credential.host,
        expires_at=credential.expires_at,
        scopes=_as_scopes(credential.scopes),
        permits_cross_project=scopes_permit_cross_project(provider, _as_scopes(credential.scopes)),
    )


def store(
    *,
    user_id: int,
    provider: GitPlatform | str,
    host: str,
    platform_uid: str,
    access_token: str,
    refresh_token: str | None = None,
    expires_at: datetime | None = None,
    scopes: Iterable[str] = (),
):
    """Persist a fresh grant as ``connected``. Called from allauth's (synchronous) login path.

    ``scopes`` records what the platform actually granted, which may be narrower than requested.

    Returns the stored row, or ``None`` when the grant was minted against a different host than
    this deployment talks to. A revoked row is left revoked and returned unchanged: withdrawing
    has to outlast the next sign-in, and :func:`clear_revoked` is the one path that undoes it.
    """
    from accounts.models import CredentialState, PlatformCredential

    provider_value = GitPlatform(provider).value

    if (minted_by := auth_hosts(provider)) and host not in minted_by:
        logger.error(
            "Refusing to store the %s credential for user_id=%s: it was issued by %s but this "
            "deployment talks to %s. Align ALLAUTH_GITLAB_URL with CODEBASE_GITLAB_URL.",
            provider_value,
            user_id,
            " / ".join(sorted(minted_by)),
            host,
        )
        return None

    existing = PlatformCredential.objects.filter(user_id=user_id, provider=provider_value, host=host).first()
    if existing is not None and existing.state == CredentialState.REVOKED:
        logger.info(
            "Leaving the revoked %s credential for user_id=%s revoked; re-authorisation is explicit.",
            provider_value,
            user_id,
        )
        return existing

    # The model rejects an expiring grant without a refresh token, so store it expired, holding no secret.
    state = CredentialState.CONNECTED
    if expires_at is not None and not refresh_token:
        logger.error(
            "The %s grant for user_id=%s carries an expiry but no refresh token, so DAIV can never "
            "renew it; storing it as expired. Check the OAuth app's configuration.",
            provider_value,
            user_id,
        )
        expires_at = None
        state = CredentialState.EXPIRED

    def _write():
        # Not get_or_create: the model rejects a row saved without its access token.
        credential = (
            PlatformCredential.objects
            .select_for_update()
            .filter(user_id=user_id, provider=provider_value, host=host)
            .first()
        ) or PlatformCredential(user_id=user_id, provider=provider_value, host=host)
        credential.platform_uid = str(platform_uid)
        credential.access_token = access_token if state == CredentialState.CONNECTED else None
        credential.refresh_token = refresh_token
        credential.expires_at = expires_at
        credential.scopes = list(scopes)
        credential.state = state
        credential.save()
        return credential

    try:
        with transaction.atomic():
            credential = _write()
    except IntegrityError:
        # Two sign-ins raced the unique key; the loser re-reads and updates.
        with transaction.atomic():
            credential = _write()

    return credential


def revoke(*, user_id: int, provider: GitPlatform | str, host: str | None = None) -> bool:
    """Withdraw a grant: clear both secrets and mark it ``revoked``. Returns whether a row changed."""
    from accounts.models import CredentialState

    return _transition(user_id=user_id, provider=provider, host=host, state=CredentialState.REVOKED)


def expire(*, user_id: int, provider: GitPlatform | str, host: str | None = None) -> bool:
    """Mark a grant unusable because renewal failed. Same shape as :func:`revoke`, different cause."""
    from accounts.models import CredentialState

    return _transition(user_id=user_id, provider=provider, host=host, state=CredentialState.EXPIRED)


def clear_revoked(*, user_id: int, provider: GitPlatform | str, host: str | None = None) -> bool:
    """Drop revoked rows for one identity so a fresh grant may be stored. Returns whether any went.

    The counterpart to ``store``'s refusal to resurrect a revoked grant: withdrawing outlasts the
    next sign-in, and only an explicit re-authorisation undoes it.
    """
    from accounts.models import CredentialState

    deleted, _ = _rows_for(user_id, provider, host).filter(state=CredentialState.REVOKED).delete()
    if deleted:
        logger.info("Cleared %s revoked %s credential row(s) for user_id=%s", deleted, provider, user_id)
    return bool(deleted)


aexpire = sync_to_async(expire)


async def arenew_rejected_token(*, user_id: int, provider: GitPlatform | str, token: str) -> CredentialReason | None:
    """Renew a grant after the platform rejected ``token``: ``None`` once the stored token is no longer ``token``.

    The refresh endpoint, not the rejection, decides whether the grant is dead. A grant with no refresh
    token cannot be renewed here, so it is left in place and :attr:`CredentialReason.REJECTED` returned.
    """
    host = platform_host(provider)
    credential = await _rows_for(user_id, provider, host).afirst()
    if credential is None:
        return CredentialReason.NO_CREDENTIAL
    if (dead := _dead_reason(credential.state)) is not None:
        return dead
    try:
        access_token = credential.access_token
    except DecryptionError:
        logger.error(
            "The %s grant for user_id=%s cannot be decrypted; expiring it. DAIV_ENCRYPTION_KEY may have rotated.",
            credential.provider,
            user_id,
        )
        await aexpire(user_id=user_id, provider=provider, host=host)
        return CredentialReason.UNREADABLE
    if access_token != token:
        return None
    if credential._refresh_token_encrypted is None:
        return CredentialReason.REJECTED
    refreshed = await _arefresh(credential)
    return refreshed if isinstance(refreshed, CredentialReason) else None


def _dead_reason(state: str) -> CredentialReason | None:
    """Why a grant in ``state`` cannot be used, or ``None`` while it is connected."""
    from accounts.models import CredentialState

    if state == CredentialState.CONNECTED:
        return None
    return CredentialReason.REVOKED if state == CredentialState.REVOKED else CredentialReason.EXPIRED


def _as_scopes(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(value.split())
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


async def _acapability_enabled() -> bool:
    return bool(await sync_to_async(lambda: site_settings.cross_project_access_enabled)())


async def _awebhook_runs_enabled() -> bool:
    return bool(await sync_to_async(lambda: site_settings.cross_project_webhook_runs_enabled)())


def _needs_refresh(credential) -> bool:
    if credential.expires_at is None:
        return False
    return credential.expires_at - timezone.now() <= timedelta(seconds=REFRESH_MARGIN_SECONDS)


def _clear_secrets(credential, state: str) -> None:
    """Strip both secrets from a locked row and set ``state``. The only place a grant dies."""
    credential.access_token = None
    credential.refresh_token = None
    # The model rejects an expiry without a refresh token, so both go together.
    credential.expires_at = None
    credential.state = state
    credential.save(
        update_fields=["_access_token_encrypted", "_refresh_token_encrypted", "expires_at", "state", "modified"]
    )


def _transition(*, user_id: int, provider: GitPlatform | str, host: str | None, state: str) -> bool:
    """Clear the secrets and set ``state``. With no ``host``, every row for the identity.

    Disconnecting is a statement about a provider, not about whichever host the settings happen to
    name today, so an unqualified call must not leave a usable grant behind.
    """
    changed = False
    with transaction.atomic():
        for credential in _rows_for(user_id, provider, host).select_for_update():
            _clear_secrets(credential, state)
            changed = True
    return changed


def _token_endpoint(provider: GitPlatform) -> str:
    if provider == GitPlatform.GITHUB:
        # The login adapter's own endpoint, so a GitHub Enterprise deployment never posts its secret to github.com.
        from accounts.socialaccount import GitHubAppOAuth2Adapter

        return GitHubAppOAuth2Adapter.access_token_endpoint()
    return f"{_gitlab_auth_base().rstrip('/')}/oauth/token"


def _oauth_client() -> dict[str, str] | None:
    """The OAuth client credentials, keyed so Sentry's scrubber recognises the secret in a frame local."""
    client_id = site_settings.auth_client_id
    secret = site_settings.auth_client_secret
    client_secret = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    if not client_id or not client_secret:
        return None
    return {"client_id": client_id, "client_secret": client_secret}


@asynccontextmanager
async def _refresh_lock(pk: int) -> AsyncGenerator[bool]:
    """Hold one grant's refresh lock across workers; yields ``False`` when it could not be had in time.

    Both platforms rotate the refresh token on use and refuse the loser of two parallel refreshes as a
    dead grant, so refreshes of one grant run one at a time.
    """
    key = f"{TOKEN_CACHE_PREFIX}:refresh:{pk}"
    owner = uuid.uuid4().hex
    deadline = time.monotonic() + REFRESH_LOCK_SECONDS
    while not await cache.aadd(key, owner, timeout=REFRESH_LOCK_SECONDS):
        if time.monotonic() >= deadline:
            yield False
            return
        await asyncio.sleep(0.1)
    try:
        yield True
    finally:
        if await cache.aget(key) == owner:
            await cache.adelete(key)


async def _arefresh(credential):
    """Renew a credential in one transaction, or return the reason it could not be renewed.

    Both platforms rotate the refresh token on every use, so a partial write leaves a credential that can
    never renew again — access token, refresh token and expiry go together or not at all. A refresh that
    finds the row changed while it waited for the lock returns the newer row instead of renewing again.
    """
    from accounts.models import PlatformCredential

    seen_modified = credential.modified
    async with _refresh_lock(credential.pk) as locked:
        if not locked:
            logger.warning(
                "Another refresh of the %s grant for user_id=%s held its lock too long; leaving the grant in place.",
                credential.provider,
                credential.user_id,
            )
            return CredentialReason.REFRESH_FAILED
        current = await PlatformCredential.objects.filter(pk=credential.pk).afirst()
        if current is None:
            return CredentialReason.NO_CREDENTIAL
        if (superseded := _superseded(current, seen_modified)) is not None:
            return superseded
        return await _arefresh_locked(current, seen_modified)


async def _arefresh_locked(credential, seen_modified):
    try:
        refresh_token = credential.refresh_token
    except DecryptionError:
        logger.error(
            "The %s refresh token for user_id=%s cannot be decrypted; expiring the grant. "
            "DAIV_ENCRYPTION_KEY may have rotated.",
            credential.provider,
            credential.user_id,
        )
        await aexpire(user_id=credential.user_id, provider=credential.provider, host=credential.host)
        return CredentialReason.UNREADABLE
    if not refresh_token:
        # Only reachable if the platform_credential_expiring_needs_refresh constraint was bypassed.
        logger.error(
            "The %s credential for user_id=%s expires but carries no refresh token; expiring it.",
            credential.provider,
            credential.user_id,
        )
        await aexpire(user_id=credential.user_id, provider=credential.provider, host=credential.host)
        return CredentialReason.EXPIRED

    payload = await _arequest_refresh(credential, refresh_token)
    if isinstance(payload, RefreshFailure):
        if payload is RefreshFailure.TRANSIENT:
            return CredentialReason.REFRESH_FAILED
        return await sync_to_async(_expire_unless_renewed)(credential.pk, seen_modified=seen_modified)

    access_token = payload.get("access_token")
    if not access_token:
        logger.warning(
            "Refresh for user_id=%s provider=%s returned no access token; leaving the grant in place.",
            credential.user_id,
            credential.provider,
        )
        return CredentialReason.REFRESH_FAILED

    refresh_token = payload.get("refresh_token") or refresh_token
    expires_in = payload.get("expires_in")
    expires_at = timezone.now() + timedelta(seconds=int(expires_in)) if expires_in not in (None, "") else None
    scopes = _as_scopes(payload.get("scope")) or _as_scopes(credential.scopes)
    return await sync_to_async(_persist_refresh)(
        credential.pk,
        seen_modified=seen_modified,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=scopes,
    )


def _superseded(credential, seen_modified):
    """What a refresh must yield instead of writing, when the row is no longer the one it read.

    A refresh request takes seconds, and a Disconnect or re-authorisation can land meanwhile.
    ``None`` means the row is untouched and the refresh may write; otherwise the newer connected
    row, or the reason the grant is dead.
    """
    if (dead := _dead_reason(credential.state)) is not None:
        return dead
    return credential if credential.modified != seen_modified else None


def _expire_unless_renewed(pk: int, *, seen_modified):
    """Clear the grant, unless the row changed while this refresh was in flight.

    GitLab rotates the refresh token on first use, so the loser of two parallel refreshes is told
    ``invalid_grant`` for a token the winner already replaced. Expiring on that word would clear
    the grant the winner just renewed.
    """
    from accounts.models import CredentialState, PlatformCredential

    with transaction.atomic():
        credential = PlatformCredential.objects.select_for_update().filter(pk=pk).first()
        if credential is None:
            return CredentialReason.NO_CREDENTIAL
        if (superseded := _superseded(credential, seen_modified)) is not None:
            return superseded
        _clear_secrets(credential, CredentialState.EXPIRED)
    return CredentialReason.EXPIRED


def _persist_refresh(pk: int, *, seen_modified, access_token: str, refresh_token: str | None, expires_at, scopes):
    from accounts.models import CredentialState, PlatformCredential

    with transaction.atomic():
        credential = PlatformCredential.objects.select_for_update().filter(pk=pk).first()
        if credential is None:
            return CredentialReason.NO_CREDENTIAL
        if (superseded := _superseded(credential, seen_modified)) is not None:
            return superseded
        credential.access_token = access_token
        credential.refresh_token = refresh_token
        credential.expires_at = expires_at
        credential.scopes = list(scopes)
        credential.state = CredentialState.CONNECTED
        credential.save(
            update_fields=[
                "_access_token_encrypted",
                "_refresh_token_encrypted",
                "expires_at",
                "scopes",
                "state",
                "modified",
            ]
        )
    return credential


async def _arequest_refresh(credential, refresh_token: str) -> dict[str, Any] | RefreshFailure:
    """POST the refresh grant. Returns the parsed payload, or why the attempt failed.

    Nothing from the response body is logged beyond the OAuth ``error`` code: an error body
    routinely echoes the token.
    """
    client = _oauth_client()
    if client is None:
        # Transient: a missing client must not destroy the grants it cannot renew.
        logger.error("Cannot refresh platform credentials: no OAuth client is configured.")
        return RefreshFailure.TRANSIENT

    provider = GitPlatform(credential.provider)
    data = {**client, "grant_type": "refresh_token", "refresh_token": refresh_token}
    try:
        async with httpx.AsyncClient(timeout=REFRESH_TIMEOUT_SECONDS) as http:
            response = await http.post(
                _token_endpoint(provider), data=data, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
            )
    except httpx.HTTPError:
        # No exception chaining into the log: httpx puts the request body in some error reprs.
        logger.warning(
            "Refreshing the %s credential for user_id=%s failed at the transport layer.",
            credential.provider,
            credential.user_id,
        )
        return RefreshFailure.TRANSIENT

    try:
        payload = response.json()
    except ValueError:
        payload = None

    # GitHub reports refresh failures as HTTP 200 with an ``error`` key, so the body decides, not the status.
    error = payload.get("error") if isinstance(payload, dict) else None
    if error in TERMINAL_OAUTH_ERRORS:
        logger.error(
            "The %s grant for user_id=%s was refused as %s; clearing it.",
            credential.provider,
            credential.user_id,
            error,
        )
        return RefreshFailure.TERMINAL

    if error or response.status_code >= 400 or not isinstance(payload, dict):
        logger.warning(
            "Refreshing the %s credential for user_id=%s did not succeed (HTTP %s, error=%s); "
            "leaving the grant in place.",
            credential.provider,
            credential.user_id,
            response.status_code,
            error or "-",
        )
        return RefreshFailure.TRANSIENT
    return payload
