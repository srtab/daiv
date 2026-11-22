import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.core.cache import cache

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from ninja.errors import AuthenticationError

from accounts.api.ci_identity import CIIdentityBearer, CIIdentityRefused, CIIdentityTokens, CIIdentityVerifier
from codebase.base import GitPlatform, Repository
from codebase.conf import settings as codebase_settings
from core.conf import settings as core_settings

AUDIENCE = "https://daiv.example.com"
GITLAB = "https://gitlab.example.com"


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY = _rsa_key()
OTHER_KEY = _rsa_key()


def _jwks():
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(SIGNING_KEY.public_key(), as_dict=True)
    return {"keys": [jwk | {"kid": "k1", "use": "sig", "alg": "RS256"}]}


def gitlab_claims(**claims):
    now = int(time.time())
    return {
        "iss": GITLAB,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "jti": "jti-1",
        "project_id": "22",
        "project_path": "group/project",
        "user_id": "9",
        "user_login": "pusher",
        "sha": "abc123",
    } | claims


def github_claims(**claims):
    now = int(time.time())
    return {
        "iss": "https://token.actions.githubusercontent.com",
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "jti": "jti-2",
        "repository_id": "33",
        "repository": "octo/repo",
        "actor_id": "12",
        "actor": "octocat",
    } | claims


def token(claims, *, key=SIGNING_KEY, algorithm="RS256", kid="k1"):
    if algorithm.startswith("HS"):
        return jwt.encode(claims, "a-shared-secret-of-at-least-32-bytes!", algorithm=algorithm, headers={"kid": kid})
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


@pytest.fixture(autouse=True)
def platform():
    """GitLab at ``GITLAB``, DAIV at ``AUDIENCE``, and signing keys served from memory."""
    CIIdentityVerifier._key_clients.clear()
    with (
        patch.object(codebase_settings, "CLIENT", GitPlatform.GITLAB),
        patch.object(codebase_settings, "GITLAB_URL", GITLAB),
        patch.object(core_settings, "EXTERNAL_URL", f"{AUDIENCE}/"),
        patch.object(core_settings, "JOBS_CI_IDENTITY_AUDIENCE", None),
        patch.object(jwt.PyJWKClient, "fetch_data", return_value=_jwks()) as fetch,
    ):
        yield fetch
    CIIdentityVerifier._key_clients.clear()


class TestVerify:
    def test_a_gitlab_token_names_its_pipeline(self):
        identity = CIIdentityVerifier().verify(token(gitlab_claims()))

        assert (identity.provider, identity.repo_id, identity.repo_platform_id) == (
            GitPlatform.GITLAB, "group/project", 22,
        )  # fmt: skip
        assert (identity.actor_uid, identity.actor_username, identity.sha, identity.jti) == (
            "9", "pusher", "abc123", "jti-1",
        )  # fmt: skip
        assert identity.pk == "ci:gitlab:9"
        assert identity.user is None

    @pytest.mark.parametrize(("sha", "expected"), [({"sha": "def456"}, "def456"), ({}, None)], ids=["sha", "no-sha"])
    def test_a_github_token_names_its_workflow_run(self, platform, sha, expected):
        claims = github_claims(**sha)
        with patch.object(codebase_settings, "CLIENT", GitPlatform.GITHUB):
            identity = CIIdentityVerifier().verify(token(claims))

        assert (identity.provider, identity.repo_id, identity.repo_platform_id) == (GitPlatform.GITHUB, "octo/repo", 33)
        assert (identity.actor_uid, identity.actor_username, identity.sha) == ("12", "octocat", expected)
        assert identity.pk == "ci:github:12"
        assert platform.call_count == 1

    def test_a_token_within_the_leeway_is_accepted(self):
        claims = gitlab_claims(exp=int(time.time()) - 30)

        assert CIIdentityVerifier().verify(token(claims)).jti == "jti-1"

    @pytest.mark.parametrize(
        ("raw", "check"),
        [
            (lambda: token(gitlab_claims(aud="https://elsewhere.example.com")), "audience"),
            (lambda: token(gitlab_claims(iss="https://gitlab.com")), "issuer"),
            (lambda: token(gitlab_claims(exp=int(time.time()) - 120)), "expired"),
            (lambda: token(gitlab_claims(), algorithm="HS256"), "algorithm"),
            (lambda: token(gitlab_claims(), key=OTHER_KEY), "signature"),
            (lambda: token({k: v for k, v in gitlab_claims().items() if k != "jti"}), "missing claim"),
            (lambda: token({k: v for k, v in gitlab_claims().items() if k != "project_id"}), "missing claim"),
            (lambda: token(gitlab_claims(), kid="unknown"), "signature"),
        ],
        ids=["audience", "issuer", "expired", "algorithm", "signature", "no-jti", "no-repository", "unknown-key"],
    )
    def test_a_token_failing_a_check_is_refused_by_that_check(self, raw, check):
        with pytest.raises(CIIdentityRefused) as refused:
            CIIdentityVerifier().verify(raw())

        assert (refused.value.check, refused.value.claimed_repo) == (check, "group/project")

    def test_unreachable_signing_keys_refuse_and_log_an_error(self, platform, caplog):
        platform.side_effect = jwt.PyJWKClientConnectionError("down")

        with caplog.at_level(logging.WARNING), pytest.raises(CIIdentityRefused):
            CIIdentityVerifier().verify(token(gitlab_claims()))

        assert [record.levelno for record in caplog.records] == [logging.ERROR]

    def test_the_github_issuer_is_the_configured_one(self):
        with (
            patch.object(codebase_settings, "CLIENT", GitPlatform.GITHUB),
            patch.object(codebase_settings, "GITHUB_ACTIONS_OIDC_ISSUER", "https://ghe.example.com/_services/token"),
        ):
            assert CIIdentityVerifier().issuer == "https://ghe.example.com/_services/token"

    def test_the_gitlab_issuer_is_the_instance_or_gitlab_com(self):
        assert CIIdentityVerifier().issuer == GITLAB
        with patch.object(codebase_settings, "GITLAB_URL", None):
            assert CIIdentityVerifier().issuer == "https://gitlab.com"

    def test_the_audience_setting_replaces_daivs_address(self):
        with patch.object(core_settings, "JOBS_CI_IDENTITY_AUDIENCE", "daiv-ci"):
            assert CIIdentityVerifier().verify(token(gitlab_claims(aud="daiv-ci"))).jti == "jti-1"


class TestVerifyRepository:
    def _identity(self):
        return CIIdentityVerifier().verify(token(gitlab_claims()))

    def _repository(self, pk):
        return Repository(
            pk=pk, slug="group/project", name="project", default_branch="main", git_platform=GitPlatform.GITLAB,
            clone_url="https://g/group/project.git", html_url="https://g/group/project",
        )  # fmt: skip

    def test_the_repository_whose_id_the_token_names_passes(self, mock_repo_client):
        mock_repo_client.get_repository.return_value = self._repository(22)

        CIIdentityVerifier().verify_repository(self._identity(), "group/project")

    def test_another_repository_is_refused(self, mock_repo_client):
        mock_repo_client.get_repository.return_value = self._repository(23)

        with pytest.raises(CIIdentityRefused) as refused:
            CIIdentityVerifier().verify_repository(self._identity(), "group/project-fork")

        assert refused.value.check == "repository"


class TestBearer:
    @pytest.mark.parametrize("key", ["daiv_abc123", "not.a.jwt", None])
    async def test_a_value_that_is_no_ci_token_is_left_to_the_api_key(self, key):
        assert await CIIdentityBearer().authenticate(SimpleNamespace(), key) is None

    async def test_a_valid_token_authenticates_its_linked_user(self):
        user = SimpleNamespace(pk=5)
        with patch("accounts.api.ci_identity.resolve_linked_user", new=AsyncMock(return_value=user)) as linked:
            identity = await CIIdentityBearer().authenticate(SimpleNamespace(), token(gitlab_claims()))

        linked.assert_awaited_once_with(GitPlatform.GITLAB, "9")
        assert identity.user is user

    async def test_a_refused_token_is_unauthorized_and_logged_without_the_token(self, caplog):
        raw = token(gitlab_claims(aud="https://elsewhere.example.com"))

        with caplog.at_level(logging.WARNING), pytest.raises(AuthenticationError):
            await CIIdentityBearer().authenticate(SimpleNamespace(), raw)

        [record] = caplog.records
        assert record.levelno == logging.WARNING
        assert "audience" in record.getMessage()
        assert "group/project" in record.getMessage()
        assert all(raw not in r.getMessage() and raw.split(".")[1] not in r.getMessage() for r in caplog.records)


class TestTokens:
    @pytest.fixture(autouse=True)
    async def empty_cache(self):
        await cache.aclear()

    def _identity(self, **claims):
        return CIIdentityVerifier().verify(token(gitlab_claims(**claims)))

    async def test_a_token_is_claimed_once(self, caplog):
        identity = self._identity()

        with caplog.at_level(logging.WARNING):
            assert await CIIdentityTokens.aclaim(identity) is True
            assert await CIIdentityTokens.aclaim(identity) is False

        assert "reused" in caplog.text

    async def test_two_concurrent_claims_let_one_through(self):
        identity = self._identity()

        assert sorted(await asyncio.gather(CIIdentityTokens.aclaim(identity), CIIdentityTokens.aclaim(identity))) == [
            False,
            True,
        ]

    async def test_a_released_claim_can_be_made_again(self):
        identity = self._identity()
        await CIIdentityTokens.aclaim(identity)

        await CIIdentityTokens.arelease(identity)

        assert await CIIdentityTokens.aclaim(identity) is True

    async def test_the_claim_lasts_until_the_token_expires_plus_the_leeway(self):
        identity = self._identity(exp=int(time.time()) + 300)

        with patch("accounts.api.ci_identity.cache.aadd", new=AsyncMock(return_value=True)) as add:
            await CIIdentityTokens.aclaim(identity)

        assert add.await_args.args[0] == "jobs:ci-token:gitlab:jti-1"
        assert 355 <= add.await_args.args[2] <= 360

    async def test_an_unreachable_cache_refuses_and_logs_an_error(self, caplog):
        with (
            patch("accounts.api.ci_identity.cache.aadd", new=AsyncMock(side_effect=ConnectionError("redis down"))),
            caplog.at_level(logging.WARNING),
        ):
            assert await CIIdentityTokens.aclaim(self._identity()) is False

        assert [record.levelno for record in caplog.records] == [logging.ERROR]
