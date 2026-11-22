"""CI identity tokens: the short-lived token a git platform signs for one CI job, accepted by the jobs API in place
of an API key.

GitLab CI ID tokens and GitHub Actions OIDC tokens are JWTs signed with keys the platform publishes. A token is
accepted only when one of those keys verifies its ``RS256`` signature, its issuer is the configured platform, its
audience is DAIV's address and it has not expired (60 s of leeway), and it names a repository, the triggering account
and its own id (``jti``). The jobs API then checks that the token's repository is the job's, by the stable id the
platform reports, and claims the token so it submits one accepted job.

Every refusal logs once at WARNING with the failed check and the repository path the token claims, never the token;
the caller is told only ``401``. Signing keys or a claim store that can't be reached refuse too, and log at ERROR.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from django.core.cache import cache
from django.utils import timezone

import jwt
from asgiref.sync import sync_to_async
from ninja.errors import AuthenticationError
from ninja.security import HttpBearer

from accounts.utils import resolve_linked_user
from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.clients.base import PLATFORM_ERRORS, is_not_found
from codebase.conf import settings as codebase_settings
from core.conf import settings as core_settings

if TYPE_CHECKING:
    from django.http import HttpRequest

    from accounts.models import User
    from codebase.base import Repository

logger = logging.getLogger("daiv.accounts")

ALGORITHMS = ["RS256"]
LEEWAY_SECONDS = 60
GITLAB_ISSUER = "https://gitlab.com"

_CLAIMS = {
    GitPlatform.GITLAB: {"repo_path": "project_path", "repo_id": "project_id", "uid": "user_id", "login": "user_login"},
    GitPlatform.GITHUB: {"repo_path": "repository", "repo_id": "repository_id", "uid": "actor_id", "login": "actor"},
}
KEYS_UNREACHABLE = "signing keys unreachable"
# First match wins: PyJWKClientConnectionError is a PyJWKClientError, and every class here a PyJWTError.
_REFUSALS: tuple[tuple[type[jwt.PyJWTError] | tuple[type[jwt.PyJWTError], ...], str], ...] = (
    (jwt.PyJWKClientConnectionError, KEYS_UNREACHABLE),
    ((jwt.PyJWKClientError, jwt.InvalidSignatureError), "signature"),
    (jwt.InvalidAlgorithmError, "algorithm"),
    (jwt.InvalidIssuerError, "issuer"),
    (jwt.InvalidAudienceError, "audience"),
    ((jwt.ExpiredSignatureError, jwt.ImmatureSignatureError, jwt.InvalidIssuedAtError), "expired"),
    (jwt.MissingRequiredClaimError, "missing claim"),
    (jwt.PyJWTError, "malformed"),
)


class CIIdentityRefused(Exception):  # noqa: N818
    """A CI identity token was refused by the check ``check`` names; ``claimed_repo`` is the repository path it
    claims, unverified before the signature is."""

    def __init__(self, check: str, claimed_repo: str = ""):
        super().__init__(check)
        self.check = check
        self.claimed_repo = claimed_repo

    def log(self) -> None:
        logger.warning("CI identity token refused (%s) for the repository it claims: %s", self.check, self.claimed_repo)


@dataclass(frozen=True)
class CIIdentity:
    """The principal of a request a CI identity token authenticated: one pipeline of one repository, triggered by one
    platform account."""

    provider: GitPlatform
    repo_id: str
    repo_platform_id: int
    actor_uid: str
    actor_username: str
    user: User | None
    sha: str | None
    jti: str
    expires_at: datetime

    @property
    def pk(self) -> int | str:
        """The key its jobs are rate-limited on: the triggering account's DAIV user, so CI shares their budget, else
        the account itself."""
        if self.user is not None:
            return self.user.pk
        return f"ci:{self.provider}:{self.actor_uid}"


class CIIdentityVerifier:
    """Verifies the CI identity tokens of the configured git platform against the signing keys it publishes."""

    _key_clients: ClassVar[dict[str, jwt.PyJWKClient]] = {}

    def __init__(self, provider: GitPlatform | None = None):
        self.provider = GitPlatform(provider or codebase_settings.CLIENT)

    @property
    def issuer(self) -> str:
        if self.provider == GitPlatform.GITHUB:
            return codebase_settings.GITHUB_ACTIONS_OIDC_ISSUER.rstrip("/")
        return (codebase_settings.GITLAB_ID_TOKEN_ISSUER or self._gitlab_url).rstrip("/")

    @property
    def keys_url(self) -> str:
        """Where the platform publishes its signing keys: GitLab's at the address DAIV reaches it, which may differ from
        the issuer its tokens name."""
        if self.provider == GitPlatform.GITHUB:
            return f"{self.issuer}/.well-known/jwks"
        return f"{self._gitlab_url.rstrip('/')}/oauth/discovery/keys"

    @property
    def _gitlab_url(self) -> str:
        return str(codebase_settings.GITLAB_URL or GITLAB_ISSUER)

    @property
    def audiences(self) -> list[str]:
        """DAIV's address, with and without the trailing slash a URL setting may carry."""
        audience = (core_settings.JOBS_CI_IDENTITY_AUDIENCE or str(core_settings.EXTERNAL_URL)).rstrip("/")
        return [audience, f"{audience}/"]

    def verify(self, token: str) -> CIIdentity:
        """The identity ``token`` proves. Raises :class:`CIIdentityRefused` naming the check it failed. SYNC ONLY."""
        claimed_repo = self._claimed_repo(token)
        try:
            if jwt.get_unverified_header(token).get("alg") not in ALGORITHMS:
                raise CIIdentityRefused("algorithm", claimed_repo)
            key = self._key_client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=ALGORITHMS,
                audience=self.audiences,
                issuer=self.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["iss", "aud", "exp", "iat", "jti"]},
            )
        except jwt.PyJWTError as err:
            check = next(check for errors, check in _REFUSALS if isinstance(err, errors))
            if check == KEYS_UNREACHABLE:
                logger.exception("Could not fetch the CI identity signing keys at %s", self.keys_url)
                raise CIIdentityRefused(check, claimed_repo) from None
            raise CIIdentityRefused(check, claimed_repo) from err
        return self._identity(claims)

    async def averify(self, token: str) -> CIIdentity:
        identity = await sync_to_async(self.verify, thread_sensitive=False)(token)
        return replace(identity, user=await resolve_linked_user(self.provider, identity.actor_uid))

    def verify_repository(self, identity: CIIdentity, repo_id: str) -> Repository:
        """``repo_id``'s repository, once it is the one ``identity``'s pipeline belongs to, by the stable id the
        platform reports for it; a fork's pipeline names the fork. Raises :class:`CIIdentityRefused` otherwise. SYNC
        ONLY."""
        try:
            repository = RepoClient.create_instance().get_repository(repo_id)
        except PLATFORM_ERRORS as err:
            if not is_not_found(err):
                logger.exception("Could not read repository %s to check a CI identity token", repo_id)
            raise CIIdentityRefused("repository", identity.repo_id) from err
        if repository.pk != identity.repo_platform_id:
            raise CIIdentityRefused("repository", identity.repo_id)
        return repository

    async def averify_repository(self, identity: CIIdentity, repo_id: str) -> Repository:
        return await sync_to_async(self.verify_repository, thread_sensitive=False)(identity, repo_id)

    def _identity(self, claims: dict[str, Any]) -> CIIdentity:
        names = _CLAIMS[self.provider]
        try:
            repo_path, repo_id = claims[names["repo_path"]], int(claims[names["repo_id"]])
            uid, login = str(claims[names["uid"]]), claims[names["login"]]
        except KeyError, TypeError, ValueError:
            raise CIIdentityRefused("missing claim", str(claims.get(names["repo_path"], ""))) from None
        return CIIdentity(
            provider=self.provider,
            repo_id=repo_path,
            repo_platform_id=repo_id,
            actor_uid=uid,
            actor_username=login,
            user=None,
            sha=claims.get("sha"),
            jti=str(claims["jti"]),
            expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
        )

    def _claimed_repo(self, token: str) -> str:
        """The repository path the unverified token claims, for the refusal log only."""
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return ""
        return str(claims.get(_CLAIMS[self.provider]["repo_path"], ""))

    def _key_client(self) -> jwt.PyJWKClient:
        url = self.keys_url
        if url not in self._key_clients:
            self._key_clients[url] = jwt.PyJWKClient(url, cache_keys=True, timeout=10)
        return self._key_clients[url]


class CIIdentityBearer(HttpBearer):
    """Authenticates a request whose bearer value is a CI identity token, and leaves any other value (an API key) to
    the next authentication class."""

    async def authenticate(self, request: HttpRequest, token: str | None) -> CIIdentity | None:
        if token is None or not self._looks_like_a_token(token):
            return None
        try:
            return await CIIdentityVerifier().averify(token)
        except CIIdentityRefused as refused:
            if refused.check != KEYS_UNREACHABLE:
                refused.log()
            raise AuthenticationError from None

    @staticmethod
    def _looks_like_a_token(key: str) -> bool:
        """A three-segment JWT whose header names a signing key; an API key never is one."""
        if key.count(".") != 2:
            return False
        try:
            return "kid" in jwt.get_unverified_header(key)
        except jwt.PyJWTError:
            return False


class CIIdentityTokens:
    """The CI identity tokens that submitted an accepted job, each claimed in the cache until it expires."""

    @staticmethod
    def _key(identity: CIIdentity) -> str:
        return f"jobs:ci-token:{identity.provider}:{identity.jti}"

    @classmethod
    async def aclaim(cls, identity: CIIdentity) -> bool:
        """Claim ``identity``'s token for one submission; ``False`` when it was claimed already, or the cache can't be
        reached."""
        timeout = max(int((identity.expires_at - timezone.now()).total_seconds()) + LEEWAY_SECONDS, 1)
        try:
            claimed = await cache.aadd(cls._key(identity), True, timeout)
        except Exception:
            logger.exception("Could not record the use of a CI identity token for %s", identity.repo_id)
            return False
        if not claimed:
            CIIdentityRefused("reused", identity.repo_id).log()
        return claimed

    @classmethod
    async def arelease(cls, identity: CIIdentity) -> None:
        """Release ``identity``'s claim after its submission accepted no job, so a retry with the token works."""
        try:
            await cache.adelete(cls._key(identity))
        except Exception:
            logger.exception("Could not release the claim of a CI identity token for %s", identity.repo_id)
