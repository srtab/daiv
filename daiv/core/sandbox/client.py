from __future__ import annotations

import logging
from typing import Self

import httpx

from core.conf import settings
from core.site_settings import site_settings

from .schemas import (
    ApplyMutationsRequest,
    ApplyMutationsResponse,
    RunCommandsRequest,
    RunCommandsResponse,
    StartSessionRequest,
)

logger = logging.getLogger("daiv.sandbox")


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
        """
        Start a session with the sandbox.

        Args:
            request (StartSessionRequest): The request to start the session.

        Returns:
            The session ID.
        """
        response = await self._client.post("session/", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return response.json()["session_id"]

    async def seed_session(
        self, session_id: str, repo_archive: bytes | None = None, skills_archive: bytes | None = None
    ) -> None:
        """
        Seed a session with the initial state of /repo and/or /skills.

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
        Apply a batch of file mutations to /repo on the sandbox session.

        Per-item failures are returned as `MutationResult(ok=False, error=...)`.
        Caller (e.g. the sync wrapper) is responsible for rolling back local
        state when `ok=False` or when this method raises.
        """
        response = await self._client.post(f"session/{session_id}/files/", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return ApplyMutationsResponse.model_validate(response.json())

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

    async def close_session(self, session_id: str):
        """
        Close a session with the sandbox.

        Args:
            session_id (str): The session ID.
        """
        response = await self._client.delete(f"session/{session_id}/")
        response.raise_for_status()

    def _get_headers(self) -> dict[str, str]:
        """
        Get the headers for the request.
        """
        api_key = site_settings.sandbox_api_key
        if api_key is None:
            return {}
        return {"X-API-KEY": api_key.get_secret_value()}
