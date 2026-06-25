from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import Self

import httpx

from core.conf import settings
from core.site_settings import site_settings

from .schemas import (
    ApplyMutationsRequest,
    ApplyMutationsResponse,
    FsDeleteRequest,
    FsDeleteResponse,
    FsEditRequest,
    FsEditResponse,
    FsGlobRequest,
    FsGlobResponse,
    FsGrepRequest,
    FsGrepResponse,
    FsLsRequest,
    FsLsResponse,
    FsReadRequest,
    FsReadResponse,
    FsWriteRequest,
    FsWriteResponse,
    RunCommandsRequest,
    RunCommandsResponse,
    StartSessionRequest,
)

logger = logging.getLogger("daiv.sandbox")

# Run-scoped sandbox transport. `set_runtime_ctx` opens one client per sandbox-enabled run and binds
# it here. Readers take it and inject it explicitly rather than calling it ad hoc: `create_daiv_agent`
# at graph-build time (into the backend and middlewares) and `BaseManager`'s draft-recovery path
# (into the publisher). Reading it outside an open run scope raises; there is no per-call fallback.
_run_sandbox_client: ContextVar[DAIVSandboxClient | None] = ContextVar("run_sandbox_client", default=None)


def set_run_sandbox_client(client: DAIVSandboxClient) -> Token:
    """Bind the run-scoped sandbox client; returns the token for ``reset_run_sandbox_client``."""
    return _run_sandbox_client.set(client)


def reset_run_sandbox_client(token: Token) -> None:
    """Unbind the run-scoped sandbox client previously set with ``set_run_sandbox_client``."""
    _run_sandbox_client.reset(token)


def get_run_sandbox_client() -> DAIVSandboxClient:
    """Return the run-scoped sandbox client opened by ``set_runtime_ctx``.

    Raises ``RuntimeError`` when called outside a sandbox-enabled run: sandbox-mode wiring relies on
    the transport being owned by the run, and there is no per-call fallback client.
    """
    client = _run_sandbox_client.get()
    if client is None:
        raise RuntimeError(
            "No run-scoped sandbox client. `set_runtime_ctx` opens one for sandbox-enabled runs; "
            "this code path ran without it."
        )
    return client


# Sandbox HTTP statuses a retry might clear: request-timeout (408), session-busy (409 — the
# per-session lock was held, so the operation never ran), too-early (425), rate-limit (429), and the
# transient 5xx family. Every other status — auth (401/403), session-gone (404), bad-request
# (400/422), not-implemented (501) — is permanent: a retry only burns a tool call. Shared by the bash
# tool (``SandboxMiddleware``) and the ``/workspace`` file backend so both classify a transport fault
# the same way; it lives here (the transport's own module) to keep them from drifting and to avoid an
# import cycle (``sandbox.py`` imports the backend, so the backend can't import from ``sandbox.py``).
TRANSIENT_SANDBOX_STATUS: frozenset[int] = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def is_transient_sandbox_error(exc: httpx.HTTPError) -> bool:
    """Classify an ``httpx`` error from the sandbox as transient (a retry may clear it) vs permanent.

    An ``httpx.HTTPStatusError`` is transient iff its status is in ``TRANSIENT_SANDBOX_STATUS``; any
    other ``httpx.HTTPError`` (i.e. an ``httpx.RequestError`` — no response was received: timeout,
    connection refused, network blip) is transient.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_SANDBOX_STATUS
    return True


class DAIVSandboxClient:
    """
    Client to interact with the daiv-sandbox service.

    Open the client once and reuse it for the lifetime of an agent run so a
    single ``httpx.AsyncClient`` (with its TCP+TLS connection pool) is shared
    across every call. Headers and timeout are resolved from ``site_settings``
    on open, so runtime changes propagate on the next open.
    """

    def __init__(self):
        self.url = settings.SANDBOX_URL.unicode_string()
        self._client: httpx.AsyncClient | None = None

    async def open(self) -> Self:
        """Open the underlying ``httpx.AsyncClient``. Re-entry is rejected to avoid leaking the previous client."""
        if self._client is not None:
            raise RuntimeError("DAIVSandboxClient is already open; nested entry would leak the previous httpx client")
        self._client = httpx.AsyncClient(
            base_url=self.url, headers=self._get_headers(), timeout=site_settings.sandbox_timeout
        )
        return self

    async def close(self) -> None:
        """Close the underlying ``httpx.AsyncClient``. Safe to call when not open."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> Self:
        return await self.open()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start_session(self, request: StartSessionRequest) -> str:
        """Start a session with the sandbox; returns the session ID.

        ``model_dump(mode="json")`` masks ``SecretStr`` egress secrets, but the sidecar needs the
        plaintext — so the egress block is rebuilt explicitly here (mirrors the old configure_egress
        payload construction).
        """
        payload = request.model_dump(mode="json")
        if request.egress is not None:
            payload["egress"] = {
                "policy": request.egress.policy.model_dump(mode="json"),
                "secrets": {
                    name: {"header": s.header, "value": s.value.get_secret_value()}
                    for name, s in request.egress.secrets.items()
                },
            }
        response = await self._client.post("session/", json=payload)
        response.raise_for_status()
        return response.json()["session_id"]

    async def seed_session(
        self, session_id: str, repo_archive: bytes | None = None, skills_archive: bytes | None = None
    ) -> None:
        """
        Seed a session with the initial state of /workspace/repo and/or /workspace/skills.

        One-shot per session: a 409 from the sandbox (already seeded) is treated
        as a no-op so retries and checkpoint replays are safe.
        """
        files = {
            name: (name, archive, "application/octet-stream")
            for name, archive in (("repo_archive", repo_archive), ("skills_archive", skills_archive))
            if archive is not None
        }
        if not files:
            raise ValueError("seed_session requires at least one of repo_archive or skills_archive")
        response = await self._client.post(f"session/{session_id}/seed/", files=files)
        if response.status_code == 409:
            logger.info("Sandbox session %s already seeded; skipping", session_id)
            return
        response.raise_for_status()

    async def apply_file_mutations(self, session_id: str, request: ApplyMutationsRequest) -> ApplyMutationsResponse:
        """
        Apply a batch of file mutations to /workspace/repo on the sandbox session.

        Per-item failures are returned as `MutationResult(ok=False, error=...)`; the caller
        decides how to react when `ok=False` or when this method raises.
        """
        response = await self._client.post(f"session/{session_id}/files/", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return ApplyMutationsResponse.model_validate(response.json())

    async def fs_ls(self, session_id: str, request: FsLsRequest) -> FsLsResponse:
        """List a directory under ``/workspace`` on the sandbox session."""
        response = await self._client.post(f"session/{session_id}/fs/ls", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return FsLsResponse.model_validate(response.json())

    async def fs_read(self, session_id: str, request: FsReadRequest) -> FsReadResponse:
        """Read a file under ``/workspace`` on the sandbox session."""
        response = await self._client.post(f"session/{session_id}/fs/read", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return FsReadResponse.model_validate(response.json())

    async def fs_grep(self, session_id: str, request: FsGrepRequest) -> FsGrepResponse:
        """Search file contents under ``/workspace`` on the sandbox session."""
        response = await self._client.post(f"session/{session_id}/fs/grep", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return FsGrepResponse.model_validate(response.json())

    async def fs_glob(self, session_id: str, request: FsGlobRequest) -> FsGlobResponse:
        """Glob for files under ``/workspace`` on the sandbox session."""
        response = await self._client.post(f"session/{session_id}/fs/glob", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return FsGlobResponse.model_validate(response.json())

    async def fs_write(self, session_id: str, request: FsWriteRequest) -> FsWriteResponse:
        """Write a file under ``/workspace`` on the sandbox session."""
        response = await self._client.post(f"session/{session_id}/fs/write", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return FsWriteResponse.model_validate(response.json())

    async def fs_edit(self, session_id: str, request: FsEditRequest) -> FsEditResponse:
        """Edit a file under ``/workspace`` on the sandbox session."""
        response = await self._client.post(f"session/{session_id}/fs/edit", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return FsEditResponse.model_validate(response.json())

    async def fs_delete(self, session_id: str, request: FsDeleteRequest) -> FsDeleteResponse:
        """Delete a file under ``/workspace`` on the sandbox session."""
        response = await self._client.post(f"session/{session_id}/fs/delete", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return FsDeleteResponse.model_validate(response.json())

    async def run_commands(self, session_id: str, request: RunCommandsRequest) -> RunCommandsResponse:
        """
        Run commands in the sandbox.

        Args:
            session_id (str): The session ID.
            request (RunCommandsRequest): The request to run the commands.

        Returns:
            RunCommandResponse: The response from running the commands.
        """
        response = await self._client.post(f"session/{session_id}/", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return RunCommandsResponse.model_validate(response.json())

    async def session_exists(self, session_id: str) -> bool:
        """Return True if the session container exists on the sandbox.

        Hits ``GET /session/{id}/``: a 404 means the container is gone (returns False); any other
        success status means it exists (returns True), and a non-404 error is raised. The sandbox
        currently answers 204 for a live container, restarting it if stopped — i.e. warming it for
        reuse. Semantics are owned by daiv-sandbox (see its ``GET /session/{id}/`` handler).
        """
        response = await self._client.get(f"session/{session_id}/")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    async def close_session(self, session_id: str, *, force: bool = False):
        """
        Close a session.

        By default the sandbox *stops* the container (kept warm for reuse and reclaimed later by the
        sandbox's reaper). Pass ``force=True`` to remove it immediately. Stop-vs-remove semantics
        are owned by daiv-sandbox (see its ``DELETE /session/{id}/`` handler).
        """
        response = await self._client.delete(f"session/{session_id}/", params={"force": force})
        response.raise_for_status()

    def _get_headers(self) -> dict[str, str]:
        """
        Get the headers for the request.
        """
        api_key = site_settings.sandbox_api_key
        if api_key is None:
            return {}
        return {"X-API-KEY": api_key.get_secret_value()}
