import asyncio
import base64
import io
import json
import logging
import time
import urllib.request
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.core.cache import cache

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from gitlab.exceptions import GitlabGetError
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
FETCH_DATA = jwt.PyJWKClient.fetch_data


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
        "run_attempt": "1",
    } | claims


def token(claims, *, key=SIGNING_KEY, algorithm="RS256", kid="k1"):
    if algorithm.startswith("HS"):
        return jwt.encode(claims, "a-shared-secret-of-at-least-32-bytes!", algorithm=algorithm, headers={"kid": kid})
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


def _segment(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class _KeysResponse(io.BytesIO):
    """What the signing-keys endpoint answers, in place of an HTTP response."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


@pytest.fixture(autouse=True)
def platform():
    """GitLab at ``GITLAB``, DAIV at ``AUDIENCE``, and signing keys served from memory."""
    CIIdentityVerifier._key_clients.clear()
    with (
        patch.object(codebase_settings, "CLIENT", GitPlatform.GITLAB),
        patch.object(codebase_settings, "GITLAB_URL", GITLAB),
        patch.object(codebase_settings, "GITLAB_ID_TOKEN_ISSUER", None),
        patch.object(codebase_settings, "GITHUB_URL", None),
        patch.object(codebase_settings, "GITHUB_ACTIONS_OIDC_ISSUER", None),
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

        with caplog.at_level(logging.WARNING), pytest.raises(CIIdentityRefused) as refused:
            CIIdentityVerifier().verify(token(gitlab_claims()))

        assert refused.value.check == "signing keys unreachable"
        assert [record.levelno for record in caplog.records] == [logging.ERROR]

    @pytest.mark.parametrize(
        "body", [b"<html>Sign in</html>", b'{"keys": []}', b"[1, 2]"], ids=["not-json", "no-keys", "not-an-object"]
    )
    def test_an_endpoint_without_a_usable_key_set_is_unreachable(self, body, caplog):
        with (
            patch.object(jwt.PyJWKClient, "fetch_data", FETCH_DATA),
            patch.object(urllib.request.OpenerDirector, "open", return_value=_KeysResponse(body)),
            caplog.at_level(logging.WARNING),
            pytest.raises(CIIdentityRefused) as refused,
        ):
            CIIdentityVerifier().verify(token(gitlab_claims()))

        assert (refused.value.check, refused.value.claimed_repo) == ("signing keys unreachable", "group/project")
        assert [record.levelno for record in caplog.records] == [logging.ERROR]

    def test_a_token_from_another_issuer_is_refused_before_any_key_fetch(self, platform):
        with pytest.raises(CIIdentityRefused) as refused:
            CIIdentityVerifier().verify(token(gitlab_claims(iss="https://gitlab.attacker.example.com")))

        assert refused.value.check == "issuer"
        platform.assert_not_called()

    def test_a_token_with_no_claims_to_read_is_malformed(self, platform):
        header = _segment(json.dumps({"alg": "RS256", "kid": "k1"}).encode())

        with pytest.raises(CIIdentityRefused) as refused:
            CIIdentityVerifier().verify(f"{header}.{_segment(b'not json')}.{_segment(b'signature')}")

        assert (refused.value.check, refused.value.claimed_repo) == ("malformed", "")
        platform.assert_not_called()

    @pytest.mark.parametrize(
        ("attempt", "check"), [({"run_attempt": "2"}, "re-run"), ({"run_attempt": None}, "missing claim")],
        ids=["re-run", "no-attempt"],
    )  # fmt: skip
    def test_a_github_token_of_anything_but_a_first_attempt_is_refused(self, attempt, check):
        claims = {k: v for k, v in (github_claims() | attempt).items() if v is not None}

        with patch.object(codebase_settings, "CLIENT", GitPlatform.GITHUB), pytest.raises(CIIdentityRefused) as refused:
            CIIdentityVerifier().verify(token(claims))

        assert (refused.value.check, refused.value.claimed_repo) == (check, "octo/repo")

    def test_a_gitlab_retry_is_accepted(self):
        assert CIIdentityVerifier().verify(token(gitlab_claims(run_attempt="2"))).jti == "jti-1"

    def test_the_github_issuer_is_the_configured_one(self):
        with (
            patch.object(codebase_settings, "CLIENT", GitPlatform.GITHUB),
            patch.object(codebase_settings, "GITHUB_URL", "https://github.example.com"),
            patch.object(codebase_settings, "GITHUB_ACTIONS_OIDC_ISSUER", "https://ghe.example.com/_services/token/"),
        ):
            assert CIIdentityVerifier().issuer == "https://ghe.example.com/_services/token"

    @pytest.mark.parametrize("url", [None, "https://github.com", "https://api.github.com/"])
    def test_the_github_issuer_is_github_com_s_for_github_com(self, url):
        with (
            patch.object(codebase_settings, "CLIENT", GitPlatform.GITHUB),
            patch.object(codebase_settings, "GITHUB_URL", url),
        ):
            assert CIIdentityVerifier().issuer == "https://token.actions.githubusercontent.com"

    @pytest.mark.parametrize("url", ["https://github.example.com", "https://GitHub.example.com/api/v3/"])
    def test_a_github_enterprise_server_issues_tokens_under_its_host(self, url):
        with (
            patch.object(codebase_settings, "CLIENT", GitPlatform.GITHUB),
            patch.object(codebase_settings, "GITHUB_URL", url),
        ):
            verifier = CIIdentityVerifier()

            assert verifier.issuer == "https://github.example.com/_services/token"
            assert verifier.keys_url == "https://github.example.com/_services/token/.well-known/jwks"

    @pytest.mark.parametrize("url", ["https://api.octocorp.ghe.com", "https://OctoCorp.ghe.com/"])
    def test_a_ghe_com_enterprise_issues_tokens_under_its_subdomain(self, url):
        with (
            patch.object(codebase_settings, "CLIENT", GitPlatform.GITHUB),
            patch.object(codebase_settings, "GITHUB_URL", url),
        ):
            verifier = CIIdentityVerifier()

            assert verifier.issuer == "https://token.actions.octocorp.ghe.com"
            assert verifier.keys_url == "https://token.actions.octocorp.ghe.com/.well-known/jwks"

    def test_a_github_com_token_is_refused_by_a_github_enterprise_server(self, platform):
        with (
            patch.object(codebase_settings, "CLIENT", GitPlatform.GITHUB),
            patch.object(codebase_settings, "GITHUB_URL", "https://github.example.com"),
            pytest.raises(CIIdentityRefused) as refused,
        ):
            CIIdentityVerifier().verify(token(github_claims()))

        assert refused.value.check == "issuer"

    def test_the_gitlab_issuer_is_the_instance_or_gitlab_com(self):
        assert CIIdentityVerifier().issuer == GITLAB
        with patch.object(codebase_settings, "GITLAB_URL", None):
            assert CIIdentityVerifier().issuer == "https://gitlab.com"

    def test_a_gitlab_reached_at_an_internal_address_checks_the_issuer_it_names(self, platform):
        with (
            patch.object(codebase_settings, "GITLAB_URL", "http://gitlab:8929/"),
            patch.object(codebase_settings, "GITLAB_ID_TOKEN_ISSUER", f"{GITLAB}/"),
        ):
            identity = CIIdentityVerifier().verify(token(gitlab_claims()))

        assert identity.jti == "jti-1"
        assert CIIdentityVerifier._key_clients.keys() == {"http://gitlab:8929/oauth/discovery/keys"}

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

    def test_a_repository_the_platform_does_not_find_is_refused_without_an_error(self, mock_repo_client, caplog):
        mock_repo_client.get_repository.side_effect = GitlabGetError("404 Project Not Found", response_code=404)

        with caplog.at_level(logging.WARNING), pytest.raises(CIIdentityRefused) as refused:
            CIIdentityVerifier().verify_repository(self._identity(), "group/project")

        assert refused.value.check == "repository"
        assert caplog.records == []

    def test_a_platform_failure_is_an_outage_logged_once_at_error(self, mock_repo_client, caplog):
        mock_repo_client.get_repository.side_effect = GitlabGetError("500 Internal Server Error", response_code=500)

        with caplog.at_level(logging.WARNING), pytest.raises(CIIdentityRefused) as refused:
            CIIdentityVerifier().verify_repository(self._identity(), "group/project")
        refused.value.log()

        assert (refused.value.check, refused.value.claimed_repo) == ("repository unreachable", "group/project")
        assert [record.levelno for record in caplog.records] == [logging.ERROR]


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

    async def test_unreachable_signing_keys_are_logged_once_at_error(self, platform, caplog):
        platform.side_effect = jwt.PyJWKClientConnectionError("down")

        with caplog.at_level(logging.WARNING), pytest.raises(AuthenticationError):
            await CIIdentityBearer().authenticate(SimpleNamespace(), token(gitlab_claims()))

        assert [record.levelno for record in caplog.records] == [logging.ERROR]


class TestRefused:
    def test_the_claimed_repository_is_logged_escaped_and_truncated(self, caplog):
        forged = "group/project\nCI identity token refused (forged) for the repository it claims: x" + "a" * 500

        with caplog.at_level(logging.WARNING):
            CIIdentityRefused("audience", forged).log()

        [record] = caplog.records
        message = record.getMessage()
        assert "\n" not in message
        assert repr(forged[:200]) in message
        assert len(message) < 300

    @pytest.mark.parametrize("check", ["signing keys unreachable", "repository unreachable"])
    def test_an_outage_logged_where_it_was_raised_is_not_logged_again(self, check, caplog):
        with caplog.at_level(logging.WARNING):
            CIIdentityRefused(check, "group/project").log()

        assert caplog.records == []


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

    async def test_a_failed_release_logs_an_error_and_does_not_raise(self, caplog):
        with (
            patch("accounts.api.ci_identity.cache.adelete", new=AsyncMock(side_effect=ConnectionError("redis down"))),
            caplog.at_level(logging.WARNING),
        ):
            await CIIdentityTokens.arelease(self._identity())

        assert [record.levelno for record in caplog.records] == [logging.ERROR]

    async def test_an_unreachable_cache_refuses_and_logs_an_error(self, caplog):
        with (
            patch("accounts.api.ci_identity.cache.aadd", new=AsyncMock(side_effect=ConnectionError("redis down"))),
            caplog.at_level(logging.WARNING),
        ):
            assert await CIIdentityTokens.aclaim(self._identity()) is False

        assert [record.levelno for record in caplog.records] == [logging.ERROR]
