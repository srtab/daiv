"""A run's hold on its sandbox container, from the start or warm reuse to the stop or removal.

The run executor builds one ``SandboxSession`` per sandbox-enabled run and releases it once the agent is done, however
it ended. ``SandboxMiddleware.abefore_agent`` acquires it, and everything that reaches the container goes through it:
the ``/workspace`` file backend, the ``bash`` tool, git, the publisher and draft recovery. Subagents share their
parent's session.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from core.sandbox.egress import PLATFORM_EGRESS_SECRET_NAME, with_platform_credential
from core.sandbox.schemas import StartSessionRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sandbox_envs.spec import SandboxSpec

    from codebase.clients.base import GitEgressCredential
    from core.sandbox.client import DAIVSandboxClient
    from core.sandbox.schemas import EgressConfigRequest

logger = logging.getLogger("daiv.tools")

# Couples to the detail of daiv-sandbox's POST /session/ 400 ("egress requires the egress proxy, which is not
# configured on this deployment"): the sandbox exposes no machine-readable code for it.
_EGRESS_PROXY_UNAVAILABLE_MARKER = "egress proxy"


class SandboxEgressUnavailableError(RuntimeError):
    """Raised when a session's resolved egress policy cannot be provisioned because the sandbox has
    no egress proxy configured (no shared egress CA). daiv-sandbox rejects such a session up front
    with HTTP 400 and a detail naming the egress proxy (see its ``POST /session/`` handler).
    Fail-closed: an environment that requires a restricted egress policy must not run without that
    policy in force."""


class SandboxSession:
    """One run's hold on a sandbox container.

    ``acquire`` reuses the thread's warm container when it still exists and was started from the same spec (a
    checkpoint from before fingerprints counts as the same), and otherwise starts and seeds a fresh one. Either way the
    container runs with the egress this run provisions: the environment's policy plus the git-platform rule and a
    freshly minted token. ``refresh_credential`` re-mints that token before a publish. ``release`` stops the container
    so the thread's next turn can reuse it, or removes it for a one-shot run.

    Args:
        client: The run's sandbox transport, opened by ``set_runtime_ctx``; the session never opens or closes it.
        spec: The run's sandbox spec; its fingerprint tells a warm container started from another spec.
        credential_source: Mints the git platform's egress credential; ``None`` provisions the spec's egress alone.
    """

    def __init__(
        self,
        client: DAIVSandboxClient,
        spec: SandboxSpec,
        *,
        credential_source: Callable[[], Awaitable[GitEgressCredential | None]] | None = None,
    ) -> None:
        if not spec.enabled:
            raise ValueError("A sandbox session needs a spec with a base image")
        self._client = client
        self._spec = spec
        self._credential_source = credential_source
        self._session_id: str | None = None
        self._egress: EgressConfigRequest | None = None

    @property
    def client(self) -> DAIVSandboxClient:
        return self._client

    @property
    def session_id(self) -> str | None:
        """The held container's id: ``None`` before ``acquire`` and after ``release``."""
        return self._session_id

    @property
    def is_acquired(self) -> bool:
        return self._session_id is not None

    async def acquire(
        self,
        *,
        prior_id: str | None,
        prior_fingerprint: str | None,
        seed: Callable[[], Awaitable[tuple[bytes, bytes | None]]],
    ) -> tuple[str, str]:
        """Hold a container for this run; return its id and this run's spec fingerprint, for the checkpoint.

        ``prior_id`` and ``prior_fingerprint`` are what the thread's checkpoint recorded. ``seed`` builds the repository
        and global-skills archives, and runs only for a freshly started container. Raises
        ``SandboxEgressUnavailableError`` when the sandbox has no egress proxy for an environment that needs one, and
        re-raises a failed start, or a failed seed after removing the new container.
        """
        if self._session_id is not None:
            raise RuntimeError(f"Sandbox session {self._session_id} is already acquired")
        fingerprint = self._spec.fingerprint
        egress = await self._provision_egress()
        if prior_id is not None and await self._exists(prior_id):
            if prior_fingerprint is not None and prior_fingerprint != fingerprint:
                logger.info("Sandbox environment changed since session %s started; replacing it", prior_id)
                await self._discard(prior_id, after="an environment change")
            elif await self._push_egress(prior_id, egress):
                logger.info("Reusing warm sandbox session %s", prior_id)
                self._session_id, self._egress = prior_id, egress
                return prior_id, fingerprint
            else:
                await self._discard(prior_id, after="egress refresh failure")
        session_id = await self._start(egress)
        await self._seed(session_id, seed)
        self._session_id, self._egress = session_id, egress
        return session_id, fingerprint

    async def refresh_credential(self) -> bool:
        """Re-mint the git-platform token and push it onto the held container; return whether a new one was pushed.

        Nothing is minted when the container carries no platform token (no egress proxy, or a token-less start), and
        nothing is pushed when the re-mint yields no token or the same one: GitLab's clone tokens are day-cached, so in
        practice only GitHub's per-call installation tokens rotate. Each skip is debug-logged. Raises on a failed mint
        or push.
        """
        session_id = self._session_id
        if session_id is None:
            raise RuntimeError("Refreshing the egress credential needs an acquired sandbox session")
        current = self._egress
        incumbent = current.secrets.get(PLATFORM_EGRESS_SECRET_NAME) if current is not None else None
        if current is None or incumbent is None or self._credential_source is None:
            logger.debug(
                "Not refreshing platform egress for sandbox session %s: it carries no platform token", session_id
            )
            return False
        credential = await self._credential_source()
        if credential is None or credential.value is None:
            logger.debug(
                "Not refreshing platform egress for sandbox session %s: no token could be resolved", session_id
            )
            return False
        if credential.value == incumbent.value:
            logger.debug(
                "Not refreshing platform egress for sandbox session %s: the re-mint returned the incumbent token",
                session_id,
            )
            return False
        egress = with_platform_credential(
            current, host=credential.host, header=credential.header, token=credential.value
        )
        await self._client.update_egress(session_id, egress)
        self._egress = egress
        return True

    async def release(self, *, resumable: bool) -> None:
        """Stop the container, keeping it for the thread's next turn (``resumable``), or remove it.

        Idempotent. A failed close is logged, never raised: the sandbox reaper reclaims a container this leaves behind.
        """
        session_id, self._session_id, self._egress = self._session_id, None, None
        if session_id is None:
            return
        try:
            await self._client.close_session(session_id, force=not resumable)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (404, 409):
                logger.debug("Sandbox session %s already closed (status=%s)", session_id, status)
            else:
                logger.exception(
                    "Sandbox session %s close returned status=%s; container may have leaked", session_id, status
                )
        except httpx.RequestError:
            logger.exception("Sandbox session %s close request failed; container may have leaked", session_id)

    async def _provision_egress(self) -> EgressConfigRequest | None:
        """The spec's egress plus the git-platform rule.

        DAIV pushes from inside the sandbox, so a network-off environment is still opened to the git host when the run
        holds a push token. Without one (evals' token-less platform) it stays isolated: there is nothing to push, and
        opening it would force the egress proxy onto hermetic runs.
        """
        credential = await self._credential_source() if self._credential_source is not None else None
        if credential is None or (self._spec.egress is None and credential.value is None):
            return self._spec.egress
        return with_platform_credential(
            self._spec.egress, host=credential.host, header=credential.header, token=credential.value
        )

    async def _exists(self, session_id: str) -> bool:
        """Whether ``session_id`` still exists on the sandbox, restarting it if stopped.

        ``session_exists`` maps a 404 (container gone) to ``False``, so an error here means the session could not be
        confirmed, not that it is gone. That soft-fails to ``False``, so a flaky check starts a fresh container instead
        of failing the run; the prior one may still be alive, so it is logged as possibly leaked.
        """
        try:
            return await self._client.session_exists(session_id)
        except httpx.HTTPError:
            logger.exception(
                "Could not validate sandbox session %s for reuse; creating a fresh session. The prior "
                "container may have leaked and will be reclaimed by the sandbox reaper.",
                session_id,
            )
            return False

    async def _push_egress(self, session_id: str, egress: EgressConfigRequest | None) -> bool:
        """Push this run's egress onto a warm container before reusing it; return whether it may be reused.

        The container's proxy still injects the token minted when an earlier run started it, which expires in a day or
        so. ``None`` (a token-less or network-off run) has nothing to push. A failed push (a sandbox too old for the
        route → 404, a container without the egress proxy → 409, a transport error) returns ``False``, so the caller
        starts a fresh container, whose start carries a valid token. Only ``httpx`` errors degrade that way.
        """
        if egress is None:
            return True
        try:
            await self._client.update_egress(session_id, egress)
        except httpx.HTTPError:
            logger.warning(
                "Egress refresh failed for warm sandbox session %s; recreating the session instead",
                session_id,
                exc_info=True,
            )
            return False
        return True

    async def _discard(self, session_id: str, *, after: str) -> None:
        """Remove a warm container this run will not reuse, instead of leaving it to the reaper."""
        try:
            await self._client.close_session(session_id, force=True)
        except httpx.HTTPError:
            logger.warning("Failed to stop stale sandbox session %s after %s", session_id, after, exc_info=True)

    async def _start(self, egress: EgressConfigRequest | None) -> str:
        spec = self._spec
        try:
            return await self._client.start_session(
                StartSessionRequest(
                    base_image=spec.base_image,
                    egress=egress,
                    memory_bytes=spec.memory_bytes,
                    cpus=spec.cpus,
                    environment=spec.env_vars or None,
                )
            )
        except httpx.HTTPStatusError as exc:
            detail = (exc.response.text or "").lower()
            if exc.response.status_code == 400 and _EGRESS_PROXY_UNAVAILABLE_MARKER in detail:
                logger.error(
                    "Sandbox rejected egress-required session (400): the egress proxy/CA is not configured "
                    "on the sandbox deployment. Aborting run (fail-closed)."
                )
                raise SandboxEgressUnavailableError(
                    "The resolved sandbox environment requires the egress proxy, but the sandbox rejected "
                    "the session (400). Configure the shared egress CA (DAIV_SANDBOX_EGRESS_CA_CERT_FILE + "
                    "DAIV_SANDBOX_EGRESS_CA_KEY_FILE) on the sandbox deployment to enable the egress proxy."
                ) from exc
            raise

    async def _seed(self, session_id: str, seed: Callable[[], Awaitable[tuple[bytes, bytes | None]]]) -> None:
        try:
            repo_archive, skills_archive = await seed()
            await self._client.seed_session(session_id, repo_archive=repo_archive, skills_archive=skills_archive)
        # BaseException: a stopped chat turn cancels mid-seed, and the new container must not outlive it.
        except BaseException:
            logger.exception("Failed to build or seed sandbox session %s", session_id)
            try:
                await self._client.close_session(session_id, force=True)
            except Exception:
                logger.exception("Failed to close session %s after seed failure", session_id)
            raise
