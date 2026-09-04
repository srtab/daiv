"""Resolution, refresh and revocation of per-user git platform OAuth credentials.

The only module permitted to decrypt :attr:`accounts.models.PlatformCredential.access_token`.
Every other caller asks here and receives either a usable token or a typed
:class:`CredentialReason`; the contract is
``specs/001-cross-project-user-tokens/contracts/credential-store.md``.

Five invariants this module exists to keep:

* the token cache is keyed on the **identity**, never the thread — a resumed thread whose acting
  person differs must not read the previous person's token;
* a refresh writes access token, refresh token and expiry in **one transaction**, because GitLab
  rotates the refresh token on every use;
* refresh happens at the point of use, within :data:`REFRESH_MARGIN_SECONDS` of expiry, not once
  per run — a long run outlives a 2-hour GitLab token;
* clearing a grant is **irreversible**, so only the platform naming it dead does it. A transport
  error, a 5xx or a rotated-token race yields :attr:`CredentialReason.REFRESH_FAILED` and leaves
  the row alone; every path that does clear a grant logs at ``error``, because a person losing
  their authorisation is an operator-visible event;
* no token, and no fragment of one, reaches a log record or a ``repr``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

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
    from collections.abc import Iterable, Sequence

logger = logging.getLogger("daiv.accounts")


REFRESH_MARGIN_SECONDS = 300
"""Renew a token that expires within this window. Wider than one CLI call's 30-second timeout
plus retries, narrow enough not to refresh on every call."""

TOKEN_CACHE_PREFIX = "platform_credential_token"  # noqa: S105
TOKEN_CACHE_MAX_TTL_SECONDS = 300
REFRESH_TIMEOUT_SECONDS = 15

GITLAB_CROSS_PROJECT_SCOPES = ("api", "read_api")
"""Either is enough to read another project; only ``api`` can also write there, which GitLab
itself enforces. A grant holding neither is the pre-existing ``read_user``-only authorisation
(FR-010) — refused here so the person is told to re-authorise instead of seeing a bare 403."""


class CredentialReason(StrEnum):
    """Why no token can be issued. One reason per cause — never collapsed into a generic failure,
    because the refusal the person reads has to name which thing is wrong."""

    DISABLED = "disabled"
    NO_ACTING_USER = "no_acting_user"
    NO_CREDENTIAL = "no_credential"
    EXPIRED = "expired"
    REVOKED = "revoked"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    REFRESH_FAILED = "refresh_failed"
    UNREADABLE = "unreadable"


class RefreshFailure(StrEnum):
    """Whether a failed renewal condemns the grant or only this attempt.

    Only the platform refusing the grant itself is terminal. A transport error, a 5xx or a
    malformed body says nothing about the refresh token, and clearing it on those grounds is
    unrecoverable — the person must re-authorise for what was a momentary outage.
    """

    TERMINAL = "terminal"
    TRANSIENT = "transient"


TERMINAL_OAUTH_ERRORS = frozenset({"invalid_grant"})
"""RFC 6749's one unambiguous "this refresh token is dead" code. Everything else — including
``invalid_request`` and ``server_error`` — can be our bug or the platform's, so it is transient."""


@dataclass(frozen=True, repr=False)
class ResolvedCredential:
    """Either a usable token or the reason there is none — never both."""

    token: str | None = None
    reason: CredentialReason | None = None
    scopes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.token is not None

    def __repr__(self) -> str:
        # A dataclass-generated repr is exactly how the token would reach a log record.
        return f"ResolvedCredential(ok={self.ok}, reason={self.reason})"


@dataclass(frozen=True)
class CredentialStatus:
    """What the account-settings page may show. Carries no secret."""

    connected: bool
    state: str | None = None
    host: str | None = None
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()
    permits_cross_project: bool = False


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


def token_cache_key(user_id: int, provider: GitPlatform | str, host: str) -> str:
    """Identity-derived cache key. Deliberately carries no ``thread_id`` (FR-013)."""
    return f"{TOKEN_CACHE_PREFIX}:{user_id}:{GitPlatform(provider).value}:{host}"


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
    acting_user_id: int | None,
    provider: GitPlatform | str,
    host: str | None = None,
    platform_uid: str | int | None = None,
) -> ResolvedCredential:
    """Resolve the acting person's token for ``provider``, or the typed reason there is none.

    ``platform_uid``, when given, must match the stored credential's own uid. ``resolve_user``
    matches a webhook's platform user by username and email before social uid, so it can return a
    DAIV account that never linked this platform identity — acceptable for choosing an MR
    assignee, not for choosing whose credential to spend.
    """
    if not await _acapability_enabled():
        return ResolvedCredential(reason=CredentialReason.DISABLED)

    if not acting_user_id:
        return ResolvedCredential(reason=CredentialReason.NO_ACTING_USER)

    from accounts.models import CredentialState, PlatformCredential

    host = host or platform_host(provider)
    provider_value = GitPlatform(provider).value

    credential = await PlatformCredential.objects.filter(
        user_id=acting_user_id, provider=provider_value, host=host
    ).afirst()
    if credential is None or (platform_uid is not None and str(platform_uid) != credential.platform_uid):
        return ResolvedCredential(reason=CredentialReason.NO_CREDENTIAL)

    if credential.state == CredentialState.EXPIRED:
        return ResolvedCredential(reason=CredentialReason.EXPIRED)
    if credential.state == CredentialState.REVOKED:
        return ResolvedCredential(reason=CredentialReason.REVOKED)

    scopes = _as_scopes(credential.scopes)
    if not scopes_permit_cross_project(provider, scopes):
        return ResolvedCredential(reason=CredentialReason.INSUFFICIENT_SCOPE, scopes=scopes)

    cache_key = token_cache_key(acting_user_id, provider_value, host)
    if cached := await cache.aget(cache_key):
        return ResolvedCredential(token=cached, scopes=scopes)

    if _needs_refresh(credential):
        refreshed = await _arefresh(credential)
        if isinstance(refreshed, CredentialReason):
            return ResolvedCredential(reason=refreshed)
        credential = refreshed
        scopes = _as_scopes(credential.scopes)

    try:
        token = credential.access_token
    except DecryptionError:
        # A rotated DAIV_ENCRYPTION_KEY is a documented operator action, and the ciphertext is
        # unrecoverable. Expire the row so the person is asked to re-authorise once, not on
        # every call, and so the account page stops reporting the grant as connected.
        logger.error(
            "The %s credential for user_id=%s cannot be decrypted; expiring it. DAIV_ENCRYPTION_KEY may have rotated.",
            provider_value,
            acting_user_id,
        )
        await aexpire(user_id=acting_user_id, provider=provider_value, host=host)
        return ResolvedCredential(reason=CredentialReason.UNREADABLE)

    if not token:
        return ResolvedCredential(reason=CredentialReason.EXPIRED)

    if (ttl := _cache_ttl_seconds(credential)) > 0:
        await cache.aset(cache_key, token, timeout=ttl)
    return ResolvedCredential(token=token, scopes=scopes)


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
    """
    from accounts.models import CredentialState, PlatformCredential

    # An expiring grant with no refresh token can never renew; the model rejects that
    # combination, so drop the expiry rather than write a row that will die silently.
    if expires_at is not None and not refresh_token:
        logger.warning(
            "Platform credential for user_id=%s provider=%s carries an expiry but no refresh token; "
            "storing it as non-expiring.",
            user_id,
            provider,
        )
        expires_at = None

    provider_value = GitPlatform(provider).value

    def _write():
        # Not ``get_or_create``: a row created by its ``defaults`` alone would have no access
        # token, which the model rejects. The token has to be set before the first save.
        credential = (
            PlatformCredential.objects
            .select_for_update()
            .filter(user_id=user_id, provider=provider_value, host=host)
            .first()
        ) or PlatformCredential(user_id=user_id, provider=provider_value, host=host)
        credential.platform_uid = str(platform_uid)
        credential.access_token = access_token
        credential.refresh_token = refresh_token
        credential.expires_at = expires_at
        credential.scopes = list(scopes)
        credential.state = CredentialState.CONNECTED
        credential.save()
        return credential

    try:
        with transaction.atomic():
            credential = _write()
    except IntegrityError:
        # Two sign-ins for the same person raced the unique key; the loser re-reads and updates.
        with transaction.atomic():
            credential = _write()

    cache.delete(token_cache_key(user_id, provider, host))
    return credential


def revoke(*, user_id: int, provider: GitPlatform | str, host: str | None = None) -> bool:
    """Withdraw a grant: clear both secrets and mark it ``revoked``. Returns whether a row changed."""
    return _transition(user_id=user_id, provider=provider, host=host, state=_state().REVOKED)


def expire(*, user_id: int, provider: GitPlatform | str, host: str | None = None) -> bool:
    """Mark a grant unusable because renewal failed. Same shape as :func:`revoke`, different cause."""
    return _transition(user_id=user_id, provider=provider, host=host, state=_state().EXPIRED)


def invalidate_cached_token(*, user_id: int, provider: GitPlatform | str, host: str | None = None) -> None:
    """Drop the cached token without touching the stored grant.

    A platform rejecting a token mid-call is not proof the grant is gone — only the refresh
    endpoint can say that. Dropping the cache makes the next call re-resolve, which refreshes or
    refuses on the platform's own word instead of on a guess about CLI prose.
    """
    for credential in _rows_for(user_id, provider, host).only("host"):
        cache.delete(token_cache_key(user_id, provider, credential.host))


astatus = sync_to_async(status)
arevoke = sync_to_async(revoke)
aexpire = sync_to_async(expire)
astore = sync_to_async(store)
ainvalidate_cached_token = sync_to_async(invalidate_cached_token)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _state():
    from accounts.models import CredentialState

    return CredentialState


def _as_scopes(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(value.split())
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


async def _acapability_enabled() -> bool:
    return bool(await sync_to_async(lambda: site_settings.cross_project_access_enabled)())


def _needs_refresh(credential) -> bool:
    if credential.expires_at is None:
        return False
    return credential.expires_at - timezone.now() <= timedelta(seconds=REFRESH_MARGIN_SECONDS)


def _cache_ttl_seconds(credential) -> int:
    """TTL bounded by the token's own life, so a cache hit is never a stale token."""
    if credential.expires_at is None:
        return TOKEN_CACHE_MAX_TTL_SECONDS
    remaining = (credential.expires_at - timezone.now()).total_seconds() - REFRESH_MARGIN_SECONDS
    return int(min(TOKEN_CACHE_MAX_TTL_SECONDS, max(0, remaining)))


def _transition(*, user_id: int, provider: GitPlatform | str, host: str | None, state: str) -> bool:
    """Clear the secrets and set ``state``. With no ``host``, every row for the identity.

    Disconnecting is a statement about a provider, not about whichever host the settings happen to
    name today, so an unqualified call must not leave a usable grant behind.
    """
    changed = False
    with transaction.atomic():
        for credential in _rows_for(user_id, provider, host).select_for_update():
            credential.access_token = None
            credential.refresh_token = None
            # An expiry with no refresh token is rejected by the model, and a dead row has neither.
            credential.expires_at = None
            credential.state = state
            credential.save(
                update_fields=["_access_token_encrypted", "_refresh_token_encrypted", "expires_at", "state", "modified"]
            )
            cache.delete(token_cache_key(user_id, provider, credential.host))
            changed = True
    return changed


def _token_endpoint(provider: GitPlatform) -> str:
    if provider == GitPlatform.GITHUB:
        # Reuses the login adapter's own derivation: a GitHub Enterprise deployment must not POST
        # its client secret and the person's refresh token to github.com.
        from accounts.socialaccount import GitHubAppOAuth2Adapter

        return f"{GitHubAppOAuth2Adapter._web_url()}/login/oauth/access_token"
    base = site_settings.auth_gitlab_server_url or site_settings.auth_gitlab_url
    return f"{str(base).rstrip('/')}/oauth/token"


def _oauth_client() -> tuple[str, str] | None:
    client_id = site_settings.auth_client_id
    secret = site_settings.auth_client_secret
    secret_value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    if not client_id or not secret_value:
        return None
    return client_id, secret_value


async def _arefresh(credential):
    """Renew a credential in one transaction, or return the reason it could not be renewed.

    GitLab rotates the refresh token on every use, so a partial write leaves a credential that can
    never renew again — access token, refresh token and expiry go together or not at all.
    """
    seen_modified = credential.modified
    try:
        refresh_token = credential.refresh_token
    except DecryptionError:
        return CredentialReason.UNREADABLE
    if not refresh_token:
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

    new_refresh = payload.get("refresh_token") or refresh_token
    expires_in = payload.get("expires_in")
    expires_at = timezone.now() + timedelta(seconds=int(expires_in)) if expires_in not in (None, "") else None
    scopes = _as_scopes(payload.get("scope")) or _as_scopes(credential.scopes)
    return await sync_to_async(_persist_refresh)(
        credential.pk, access_token=access_token, refresh_token=new_refresh, expires_at=expires_at, scopes=scopes
    )


def _expire_unless_renewed(pk: int, *, seen_modified):
    """Clear the grant, unless another worker renewed it while this refresh was in flight.

    GitLab rotates the refresh token on first use, so the loser of two parallel refreshes is told
    ``invalid_grant`` for a token the winner already replaced. Expiring on that word would clear
    the grant the winner just renewed.
    """
    from accounts.models import CredentialState, PlatformCredential

    with transaction.atomic():
        credential = PlatformCredential.objects.select_for_update().filter(pk=pk).first()
        if credential is None:
            return CredentialReason.NO_CREDENTIAL
        if credential.modified != seen_modified:
            return credential if credential.state == CredentialState.CONNECTED else CredentialReason.EXPIRED
        credential.access_token = None
        credential.refresh_token = None
        credential.expires_at = None
        credential.state = CredentialState.EXPIRED
        credential.save(
            update_fields=["_access_token_encrypted", "_refresh_token_encrypted", "expires_at", "state", "modified"]
        )
    cache.delete(token_cache_key(credential.user_id, credential.provider, credential.host))
    return CredentialReason.EXPIRED


def _persist_refresh(pk: int, *, access_token: str, refresh_token: str | None, expires_at, scopes):
    from accounts.models import CredentialState, PlatformCredential

    with transaction.atomic():
        credential = PlatformCredential.objects.select_for_update().get(pk=pk)
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
    cache.delete(token_cache_key(credential.user_id, credential.provider, credential.host))
    return credential


async def _arequest_refresh(credential, refresh_token: str) -> dict[str, Any] | RefreshFailure:
    """POST the refresh grant. Returns the parsed payload, or why the attempt failed.

    Nothing from the response body is logged beyond the OAuth ``error`` code: an error body
    routinely echoes the token.
    """
    client = _oauth_client()
    if client is None:
        # Deployment-wide and self-inflicted: every expiring credential hits this. Transient, so
        # it does not destroy the grants it cannot renew.
        logger.error("Cannot refresh platform credentials: no OAuth client is configured.")
        return RefreshFailure.TRANSIENT
    client_id, client_secret = client

    provider = GitPlatform(credential.provider)
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
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

    # GitHub reports refresh failures with HTTP 200 and an ``error`` key, so the body decides
    # rather than the status: only the platform naming the grant itself dead is terminal.
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
