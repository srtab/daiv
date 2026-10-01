import asyncio
import io
import tarfile
from unittest.mock import AsyncMock

import httpx
import pytest
from sandbox_envs.spec import SandboxSpec

from automation.agent.workspace.session import SandboxAcquisition, SandboxSession
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

    async def test_a_cancelled_seed_removes_the_new_container(self, caplog):
        client = FakeSandboxClient.opened()
        session = _session(client)

        with caplog.at_level("ERROR", logger="daiv.tools"), pytest.raises(asyncio.CancelledError):
            await session.acquire(
                prior_id=None, prior_fingerprint=None, seed=AsyncMock(side_effect=asyncio.CancelledError)
            )

        assert client.calls_to("close_session") == [("sess-1", True)]
        assert client.sessions == {}
        assert not session.is_acquired
        assert not caplog.records

    @pytest.mark.parametrize("method", ["session_exists", "update_egress"])
    async def test_a_cancelled_warm_reuse_stops_the_prior_container(self, method, caplog):
        """A chat Stop mid-reuse leaves the warm container the probe restarted unheld, where ``release`` cannot stop
        it: acquire stops it, keeping it for the thread's next turn, and does not log the cancellation as a failure."""
        client = FakeSandboxClient.opened()
        prior_id = await _running(client, _started_egress("turn-start"))
        client.sessions[prior_id].state = "stopped"
        session = _session(client, credential=_credential("fresh", host="gitlab.com"))
        _cancel_after(client, method)

        with caplog.at_level("ERROR", logger="daiv.tools"), pytest.raises(asyncio.CancelledError):
            await session.acquire(prior_id=prior_id, prior_fingerprint=sandbox_spec().fingerprint, seed=_seed())

        assert client.calls_to("close_session") == [(prior_id, False)]
        assert client.sessions[prior_id].state == "stopped"
        assert not session.is_acquired
        assert not caplog.records

    @pytest.mark.parametrize(
        "error", [httpx.ConnectError("refused"), RuntimeError("close bug")], ids=["transport", "bug"]
    )
    async def test_a_failed_stop_after_a_cancelled_reuse_is_logged_under_the_cancellation(self, error, caplog):
        client = FakeSandboxClient.opened()
        prior_id = await _running(client, _started_egress("turn-start"))
        session = _session(client, credential=_credential("fresh", host="gitlab.com"))
        _cancel_after(client, "session_exists")
        client.close_session = AsyncMock(side_effect=error)

        with caplog.at_level("WARNING", logger="daiv.tools"), pytest.raises(asyncio.CancelledError):
            await session.acquire(prior_id=prior_id, prior_fingerprint=None, seed=_seed())

        assert [record.levelname for record in caplog.records] == ["ERROR"]
        assert "may have leaked" in caplog.text

    async def test_a_failed_warm_reuse_stops_the_prior_container(self):
        client = FakeSandboxClient.opened()
        prior_id = await _running(client, _started_egress("turn-start"))
        client.sessions[prior_id].state = "stopped"
        client.update_egress = AsyncMock(side_effect=ValueError("unserializable egress"))
        session = _session(client, credential=_credential("fresh", host="gitlab.com"))

        with pytest.raises(ValueError, match="unserializable"):
            await session.acquire(prior_id=prior_id, prior_fingerprint=None, seed=_seed())

        assert client.calls_to("close_session") == [(prior_id, False)]
        assert client.sessions[prior_id].state == "stopped"

    async def test_an_environment_change_removes_the_warm_container_without_waking_it(self):
        client = FakeSandboxClient.opened()
        prior_id = await _running(client, None)
        client.sessions[prior_id].state = "stopped"
        session = _session(client, sandbox_spec(base_image="python:3.13"))

        session_id, _ = await session.acquire(
            prior_id=prior_id, prior_fingerprint=sandbox_spec().fingerprint, seed=_seed()
        )

        assert client.calls_to("session_exists") == []
        assert client.calls_to("close_session") == [(prior_id, True)]
        assert prior_id not in client.sessions
        assert session_id != prior_id
        assert session.acquisition is SandboxAcquisition.ENV_CHANGED

    @pytest.mark.parametrize("stops", [1, 2])
    async def test_a_start_cancelled_in_flight_removes_the_container_it_still_creates(self, stops):
        client = FakeSandboxClient.opened()
        in_flight = _in_flight(client, "start_session", acts_first=True)
        session = _session(client)

        task = asyncio.create_task(session.acquire(prior_id=None, prior_fingerprint=None, seed=_seed()))
        await _stop_in_flight(task, *in_flight, stops=stops)

        assert client.calls_to("close_session") == [("sess-1", True)]
        assert client.sessions == {}
        assert not session.is_acquired

    @pytest.mark.parametrize(
        ("answer", "leak_logged"),
        [
            pytest.param(httpx.ReadTimeout("no answer"), True, id="no-answer"),
            pytest.param(httpx.ConnectError("refused"), False, id="never-sent"),
            pytest.param(httpx.HTTPStatusError("500", request=None, response=httpx.Response(500)), False, id="refused"),
        ],
    )
    async def test_a_cancelled_start_that_then_fails_is_still_a_cancellation(self, answer, leak_logged, caplog):
        client = FakeSandboxClient.opened()
        client.start_session = AsyncMock(side_effect=answer)
        in_flight = _in_flight(client, "start_session")
        task = asyncio.create_task(_session(client).acquire(prior_id=None, prior_fingerprint=None, seed=_seed()))

        with caplog.at_level("ERROR", logger="daiv.tools"):
            await _stop_in_flight(task, *in_flight, stops=1)

        assert ("may have leaked" in caplog.text) is leak_logged

    async def test_a_reaped_container_of_a_changed_environment_is_replaced_quietly(self, caplog):
        client = FakeSandboxClient.opened()
        client.fail("close_session", status=404)
        session = _session(client, sandbox_spec(base_image="python:3.13"))

        with caplog.at_level("WARNING", logger="daiv.tools"):
            await session.acquire(prior_id="sess-gone", prior_fingerprint=sandbox_spec().fingerprint, seed=_seed())

        assert session.is_acquired
        assert not caplog.records

    async def test_a_repeated_stop_still_removes_a_cancelled_seeds_container(self):
        client = FakeSandboxClient.opened()
        in_flight = _in_flight(client, "close_session")
        session = _session(client)

        task = asyncio.create_task(
            session.acquire(prior_id=None, prior_fingerprint=None, seed=AsyncMock(side_effect=asyncio.CancelledError))
        )
        await _stop_in_flight(task, *in_flight, stops=1)

        assert client.sessions == {}

    async def test_a_repeated_stop_still_stops_a_cancelled_reuses_container(self):
        client = FakeSandboxClient.opened()
        prior_id = await _running(client, _started_egress("turn-start"))
        session = _session(client, credential=_credential("fresh", host="gitlab.com"))
        _cancel_after(client, "session_exists")
        in_flight = _in_flight(client, "close_session")

        task = asyncio.create_task(session.acquire(prior_id=prior_id, prior_fingerprint=None, seed=_seed()))
        await _stop_in_flight(task, *in_flight, stops=1)

        assert client.sessions[prior_id].state == "stopped"


class TestAcquisition:
    async def test_a_fresh_session_has_acquired_nothing(self):
        assert _session(FakeSandboxClient.opened()).acquisition is SandboxAcquisition.NOT_ACQUIRED

    async def test_a_thread_without_a_container_gets_a_new_one(self):
        session = _session(FakeSandboxClient.opened())

        await session.acquire(prior_id=None, prior_fingerprint=None, seed=_seed())

        assert session.acquisition is SandboxAcquisition.NEW

    async def test_a_live_warm_container_is_reused(self):
        client = FakeSandboxClient.opened()
        prior_id = await _running(client, None)
        session = _session(client)

        session_id, _ = await session.acquire(
            prior_id=prior_id, prior_fingerprint=sandbox_spec().fingerprint, seed=_seed()
        )

        assert (session_id, session.acquisition) == (prior_id, SandboxAcquisition.WARM)

    @pytest.mark.parametrize("status", [404, None], ids=["reaped", "unconfirmed"])
    async def test_a_container_that_is_gone_is_replaced(self, status):
        client = FakeSandboxClient.opened()
        prior_id = await _running(client, None)
        client.fail("session_exists", status=status)
        session = _session(client)

        session_id, _ = await session.acquire(prior_id=prior_id, prior_fingerprint=None, seed=_seed())

        assert session_id != prior_id
        assert session.acquisition is SandboxAcquisition.GONE

    async def test_a_container_that_refuses_the_runs_egress_is_replaced(self):
        client = FakeSandboxClient.opened()
        prior_id = await _running(client, _started_egress("turn-start"))
        client.fail("update_egress", status=409)
        session = _session(client, credential=_credential("fresh", host="gitlab.com"))

        await session.acquire(prior_id=prior_id, prior_fingerprint=None, seed=_seed())

        assert session.acquisition is SandboxAcquisition.EGRESS_FAILED

    async def test_it_outlives_the_release_for_the_runs_record(self):
        session = _session(FakeSandboxClient.opened())
        await session.acquire(prior_id=None, prior_fingerprint=None, seed=_seed())

        await session.release(resumable=True)

        assert session.acquisition is SandboxAcquisition.NEW

    async def test_a_failed_acquire_stays_not_acquired(self):
        client = FakeSandboxClient.opened()
        client.fail("start_session", status=500)
        session = _session(client)

        with pytest.raises(httpx.HTTPStatusError):
            await session.acquire(prior_id=None, prior_fingerprint=None, seed=_seed())

        assert session.acquisition is SandboxAcquisition.NOT_ACQUIRED


def _cancel_after(client: FakeSandboxClient, method: str) -> None:
    """Make ``method`` take effect and then raise ``CancelledError``, as a chat Stop landing on its request does."""
    call = getattr(client, method)

    async def _cancelled(*args, **kwargs):
        await call(*args, **kwargs)
        raise asyncio.CancelledError

    setattr(client, method, _cancelled)


def _in_flight(
    client: FakeSandboxClient, method: str, *, acts_first: bool = False
) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold ``method``'s request in flight until ``gate`` is set, setting ``reached`` once it is held.

    ``acts_first`` has the sandbox act before the response is held, as it finishes a start DAIV stopped waiting for.
    """
    call = getattr(client, method)
    reached, gate = asyncio.Event(), asyncio.Event()

    async def _held(*args, **kwargs):
        result = await call(*args, **kwargs) if acts_first else None
        reached.set()
        await gate.wait()
        return result if acts_first else await call(*args, **kwargs)

    setattr(client, method, _held)
    return reached, gate


async def _stop_in_flight(task: asyncio.Task, reached: asyncio.Event, gate: asyncio.Event, *, stops: int) -> None:
    """Cancel ``task`` ``stops`` times while its request is held, as repeated chat Stops do, then let it answer."""
    await asyncio.wait_for(reached.wait(), timeout=5)
    for _ in range(stops):
        task.cancel("stop")
        await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError, match="stop"):
        await asyncio.wait_for(task, timeout=5)


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

    @pytest.mark.parametrize("reuse", [pytest.param(False, id="fresh"), pytest.param(True, id="reused")])
    async def test_it_refreshes_the_token_acquire_provisioned(self, reuse):
        client = FakeSandboxClient.opened()
        prior_id = await _running(client, _started_egress("turn-start")) if reuse else None
        rotated = _credential("tok-2", host="gitlab.com")
        source = AsyncMock(side_effect=[_credential("tok-1", host="gitlab.com"), rotated])
        session = SandboxSession(client, sandbox_spec(), credential_source=source)
        session_id, _ = await session.acquire(prior_id=prior_id, prior_fingerprint=None, seed=_seed())

        assert await session.refresh_credential() is True

        assert _injected(client.sessions[session_id].egress) == [rotated.value.get_secret_value()]


class TestRelease:
    async def test_it_is_idempotent(self):
        client = FakeSandboxClient.opened()
        session = acquired_session(client, client.add_running_session("sess-1"))

        await session.release(resumable=True)
        await session.release(resumable=True)

        assert client.calls_to("close_session") == [("sess-1", False)]
        assert not session.is_acquired

    @pytest.mark.parametrize(("status", "leak_logged"), [(404, False), (409, True), (500, True), (None, True)])
    @pytest.mark.parametrize("resumable", [True, False])
    async def test_a_failed_close_is_logged_not_raised(self, status, leak_logged, resumable, caplog):
        client = FakeSandboxClient.opened()
        session = acquired_session(client, client.add_running_session("sess-1"))
        client.fail("close_session", status=status)

        with caplog.at_level("ERROR", logger="daiv.tools"):
            await session.release(resumable=resumable)

        assert client.calls_to("close_session") == [("sess-1", not resumable)]
        assert ("sandbox session sess-1" in caplog.text and "may have leaked" in caplog.text) is leak_logged

    async def test_it_finishes_a_cleanup_a_repeated_stop_abandoned(self):
        client = FakeSandboxClient.opened()
        reached, gate = _in_flight(client, "start_session", acts_first=True)
        session = _session(client)
        node = asyncio.create_task(session.acquire(prior_id=None, prior_fingerprint=None, seed=_seed()))
        await asyncio.wait_for(reached.wait(), timeout=5)
        node.cancel()
        await asyncio.sleep(0)

        release = asyncio.create_task(session.release(resumable=True))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(release, timeout=5)
        client.is_open = False
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(node, timeout=5)

        assert client.sessions == {}

    async def test_a_repeated_stop_does_not_interrupt_the_close(self):
        client = FakeSandboxClient.opened()
        session = acquired_session(client, client.add_running_session("sess-1"))
        in_flight = _in_flight(client, "close_session")

        task = asyncio.create_task(session.release(resumable=True))
        await _stop_in_flight(task, *in_flight, stops=2)

        assert client.sessions["sess-1"].state == "stopped"

    async def test_an_unacquired_session_closes_nothing(self):
        client = FakeSandboxClient.opened()

        await _session(client).release(resumable=False)

        assert client.calls == []
