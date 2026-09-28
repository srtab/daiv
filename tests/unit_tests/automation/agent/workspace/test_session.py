import io
import tarfile
from unittest.mock import AsyncMock

import httpx
import pytest
from sandbox_envs.spec import SandboxSpec

from automation.agent.workspace.session import SandboxSession
from codebase.clients.base import GitEgressCredential
from core.sandbox.egress import PLATFORM_EGRESS_SECRET_NAME, with_platform_credential
from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret, StartSessionRequest
from tests.unit_tests.conftest import FakeSandboxClient, acquired_session, sandbox_spec


def _repo_archive() -> bytes:
    data = b"hello\n"
    info = tarfile.TarInfo("README.md")
    info.size = len(data)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _seed() -> AsyncMock:
    return AsyncMock(return_value=(_repo_archive(), None))


def _credential(token: str | None, *, host: str = "github.com") -> GitEgressCredential:
    return GitEgressCredential.for_token(host=host, token=token)


def _session(
    client, spec: SandboxSpec | None = None, *, credential: GitEgressCredential | None = None
) -> SandboxSession:
    return SandboxSession(client, spec or sandbox_spec(), credential_source=AsyncMock(return_value=credential))


def _injected(egress: EgressConfigRequest) -> list[str]:
    return [secret.value.get_secret_value() for secret in egress.secrets.values()]


async def _started_request(spec: SandboxSpec, credential: GitEgressCredential | None) -> StartSessionRequest:
    """The request a fresh container is started with, for ``spec`` and the credential the run mints."""
    client = FakeSandboxClient.opened()
    session_id, _ = await _session(client, spec, credential=credential).acquire(
        prior_id=None, prior_fingerprint=None, seed=_seed()
    )
    return client.sessions[session_id].request


def test_a_disabled_spec_has_no_session():
    with pytest.raises(ValueError, match="base image"):
        SandboxSession(FakeSandboxClient.opened(), sandbox_spec(base_image=None))


class TestAcquire:
    async def test_a_fresh_container_starts_from_the_spec(self):
        spec = SandboxSpec(
            base_image="alpine:test",
            memory_bytes=1_234,
            cpus=2.5,
            env_vars={"X": "y"},
            egress=EgressConfigRequest(policy=EgressPolicy(default="allow")),
        )

        request = await _started_request(spec, None)

        assert (request.base_image, request.memory_bytes, request.cpus, request.environment) == (
            "alpine:test",
            1_234,
            2.5,
            {"X": "y"},
        )
        assert request.egress == spec.egress

    @pytest.mark.parametrize("token", [pytest.param("tok", id="push-token"), pytest.param(None, id="token-less")])
    async def test_a_network_off_env_reaches_the_git_host_only_for_a_push_token(self, token):
        """B9: DAIV pushes from the sandbox, so a push token opens a network-off env to the git host alone; a
        token-less run (evals) stays isolated."""
        credential = _credential(token)

        egress = (await _started_request(sandbox_spec(), credential)).egress

        if token:
            [rule] = egress.policy.rules
            assert (egress.policy.default, rule.host) == ("deny", "github.com")
            assert egress.secrets == {rule.inject: EgressSecret(header=credential.header, value=credential.value)}
        else:
            assert egress is None

    async def test_a_network_off_env_stays_isolated_without_a_credential(self):
        """B9: no derivable credential (a clone URL without a host) leaves a network-off env with no network."""
        assert (await _started_request(sandbox_spec(), None)).egress is None

    @pytest.mark.parametrize(
        ("credential", "hosts"),
        [
            pytest.param(None, ["api.example.com"], id="no-credential"),
            pytest.param(GitEgressCredential(host="github.com"), ["github.com", "api.example.com"], id="token-less"),
        ],
    )
    async def test_a_network_on_env_keeps_its_rules_without_a_push_token(self, credential, hosts):
        """Without a push token a network-on env keeps its rules, behind the git host when a credential names one."""
        spec = sandbox_spec(egress=EgressConfigRequest(policy=EgressPolicy(rules=[EgressRule(host="api.example.com")])))

        egress = (await _started_request(spec, credential)).egress

        assert [rule.host for rule in egress.policy.rules] == hosts
        assert PLATFORM_EGRESS_SECRET_NAME not in egress.secrets

    async def test_a_network_on_env_gets_the_git_host_first_with_the_push_token(self):
        spec = sandbox_spec(egress=EgressConfigRequest(policy=EgressPolicy(rules=[EgressRule(host="api.example.com")])))
        credential = _credential("tok")

        egress = (await _started_request(spec, credential)).egress

        assert [(rule.host, rule.inject) for rule in egress.policy.rules] == [
            ("github.com", PLATFORM_EGRESS_SECRET_NAME),
            ("api.example.com", None),
        ]
        assert _injected(egress) == [credential.value.get_secret_value()]

    async def test_a_new_token_is_not_an_environment_change(self):
        """A GitHub installation token is minted for every run, and the fingerprint covers the policy, never the
        secret: the next turn reuses the container and pushes the new token onto it."""
        client = FakeSandboxClient.opened()
        spec = sandbox_spec()
        first = _session(client, spec, credential=_credential("tok-1"))
        session_id, fingerprint = await first.acquire(prior_id=None, prior_fingerprint=None, seed=_seed())
        await first.release(resumable=True)

        fresh = _credential("tok-2")
        second = _session(client, spec, credential=fresh)
        acquired = await second.acquire(prior_id=session_id, prior_fingerprint=fingerprint, seed=_seed())

        assert acquired == (session_id, fingerprint)
        assert client.method_names()[-2:] == ["session_exists", "update_egress"]
        assert _injected(client.sessions[session_id].egress) == [fresh.value.get_secret_value()]

    async def test_acquiring_twice_is_refused(self):
        client = FakeSandboxClient.opened()
        session = _session(client)
        await session.acquire(prior_id=None, prior_fingerprint=None, seed=_seed())

        with pytest.raises(RuntimeError, match="already acquired"):
            await session.acquire(prior_id=None, prior_fingerprint=None, seed=_seed())


def _started_egress(token: str | None, env: EgressConfigRequest | None = None) -> EgressConfigRequest:
    credential = _credential(token, host="gitlab.com")
    return with_platform_credential(env, host=credential.host, header=credential.header, token=credential.value)


async def _running(client: FakeSandboxClient, egress: EgressConfigRequest | None) -> str:
    return await client.start_session(StartSessionRequest(base_image="python:3.12", egress=egress))


class TestRefreshCredential:
    @pytest.mark.parametrize(
        "started_with",
        [
            pytest.param(None, id="no-egress-proxy"),
            pytest.param(EgressConfigRequest(), id="no-platform-rule"),
            pytest.param(_started_egress(None), id="token-less-start"),
        ],
    )
    async def test_a_session_without_a_platform_token_mints_nothing(self, started_with):
        """B8: a session started with no platform token is never credentialed at publish time."""
        client = FakeSandboxClient.opened()
        source = AsyncMock()
        session = acquired_session(
            client, await _running(client, started_with), egress=started_with, credential_source=source
        )

        assert await session.refresh_credential() is False

        source.assert_not_awaited()
        assert client.calls_to("update_egress") == []

    @pytest.mark.parametrize(
        "remint",
        [
            pytest.param(None, id="no-credential"),
            pytest.param(_credential(None, host="gitlab.com"), id="token-less"),
            pytest.param(_credential("turn-start", host="gitlab.com"), id="same-token"),
        ],
    )
    async def test_a_remint_with_nothing_new_pushes_nothing(self, remint):
        """B8: a re-mint with no token, or the turn-start one (GitLab's day-cached token), pushes nothing."""
        client = FakeSandboxClient.opened()
        started = _started_egress("turn-start")
        source = AsyncMock(return_value=remint)
        session = acquired_session(client, await _running(client, started), egress=started, credential_source=source)

        assert await session.refresh_credential() is False

        source.assert_awaited_once()
        assert client.calls_to("update_egress") == []

    async def test_a_rotated_token_is_pushed_once(self):
        """B8: a new token reaches the container; a second refresh with the same token pushes nothing."""
        client = FakeSandboxClient.opened()
        started = _started_egress("turn-start")
        fresh = _credential("fresh", host="gitlab.com")
        session_id = await _running(client, started)
        session = acquired_session(client, session_id, egress=started, credential_source=AsyncMock(return_value=fresh))

        assert await session.refresh_credential() is True
        assert await session.refresh_credential() is False

        assert client.calls_to("update_egress") == [
            (session_id, with_platform_credential(started, host=fresh.host, header=fresh.header, token=fresh.value))
        ]

    async def test_a_failed_push_raises(self):
        """The publisher logs a failed push and publishes with the turn-start token, so the session raises it."""
        client = FakeSandboxClient.opened()
        started = _started_egress("turn-start")
        session = acquired_session(
            client,
            await _running(client, started),
            egress=started,
            credential_source=AsyncMock(return_value=_credential("fresh", host="gitlab.com")),
        )
        client.fail("update_egress", status=500)

        with pytest.raises(httpx.HTTPStatusError):
            await session.refresh_credential()

    async def test_it_needs_an_acquired_session(self):
        with pytest.raises(RuntimeError, match="acquired"):
            await _session(FakeSandboxClient.opened()).refresh_credential()


class TestRelease:
    async def test_it_is_idempotent(self):
        client = FakeSandboxClient.opened()
        session = acquired_session(client, client.add_running_session("sess-1"))

        await session.release(resumable=True)
        await session.release(resumable=True)

        assert client.calls_to("close_session") == [("sess-1", False)]
        assert not session.is_acquired

    async def test_an_unacquired_session_closes_nothing(self):
        client = FakeSandboxClient.opened()

        await _session(client).release(resumable=False)

        assert client.calls == []
