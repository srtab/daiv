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
from codebase.clients.base import PLATFORM_ERRORS
from codebase.conf import settings as codebase_settings
from core.conf import settings as core_settings

if TYPE_CHECKING:
    from django.http import HttpRequest

    from accounts.models import User

logger = logging.getLogger("daiv.accounts")

ALGORITHMS = ["RS256"]
LEEWAY_SECONDS = 60
GITLAB_ISSUER = "https://gitlab.com"

_CLAIMS = {
    GitPlatform.GITLAB: {"repo_path": "project_path", "repo_id": "project_id", "uid": "user_id", "login": "user_login"},
    GitPlatform.GITHUB: {"repo_path": "repository", "repo_id": "repository_id", "uid": "actor_id", "login": "actor"},
}
_KEYS_PATHS = {GitPlatform.GITLAB: "/oauth/discovery/keys", GitPlatform.GITHUB: "/.well-known/jwks"}
_TIME_ERRORS = (jwt.ExpiredSignatureError, jwt.ImmatureSignatureError, jwt.InvalidIssuedAtError)


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
    def pk(self) -> str:
        """The key its jobs are rate-limited on: the triggering account."""
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
        return str(codebase_settings.GITLAB_URL or GITLAB_ISSUER).rstrip("/")

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
        except jwt.PyJWKClientConnectionError:
            logger.exception("Could not fetch the CI identity signing keys of %s", self.issuer)
            raise CIIdentityRefused("signing keys unreachable", claimed_repo) from None
        except jwt.PyJWKClientError as err:
            raise CIIdentityRefused("signature", claimed_repo) from err
        except jwt.InvalidAlgorithmError as err:
            raise CIIdentityRefused("algorithm", claimed_repo) from err
        except jwt.InvalidSignatureError as err:
            raise CIIdentityRefused("signature", claimed_repo) from err
        except jwt.InvalidIssuerError as err:
            raise CIIdentityRefused("issuer", claimed_repo) from err
        except jwt.InvalidAudienceError as err:
            raise CIIdentityRefused("audience", claimed_repo) from err
        except _TIME_ERRORS as err:
            raise CIIdentityRefused("expired", claimed_repo) from err
        except jwt.MissingRequiredClaimError as err:
            raise CIIdentityRefused("missing claim", claimed_repo) from err
        except jwt.PyJWTError as err:
            raise CIIdentityRefused("malformed", claimed_repo) from err
        return self._identity(claims)

    async def averify(self, token: str) -> CIIdentity:
        identity = await sync_to_async(self.verify, thread_sensitive=False)(token)
        return replace(identity, user=await resolve_linked_user(self.provider, identity.actor_uid))

    def verify_repository(self, identity: CIIdentity, repo_id: str) -> None:
        """Raise :class:`CIIdentityRefused` unless ``repo_id`` is the repository ``identity``'s pipeline belongs to,
        by the stable id the platform reports for it; a fork's pipeline names the fork. SYNC ONLY."""
        try:
            repository = RepoClient.create_instance().get_repository(repo_id)
        except PLATFORM_ERRORS as err:
            if 404 not in (getattr(err, "response_code", None), getattr(err, "status", None)):
                logger.exception("Could not read repository %s to check a CI identity token", repo_id)
            raise CIIdentityRefused("repository", identity.repo_id) from err
        if repository.pk != identity.repo_platform_id:
            raise CIIdentityRefused("repository", identity.repo_id)

    async def averify_repository(self, identity: CIIdentity, repo_id: str) -> None:
        await sync_to_async(self.verify_repository, thread_sensitive=False)(identity, repo_id)

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
        url = f"{self.issuer}{_KEYS_PATHS[self.provider]}"
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
            if refused.check != "signing keys unreachable":
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
