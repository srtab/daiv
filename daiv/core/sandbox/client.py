import base64
import logging

import httpx

from core.conf import settings
from core.site_settings import site_settings

from .schemas import (
    ApplyMutationsRequest,
    ApplyMutationsResponse,
    RunCommandsRequest,
    RunCommandsResponse,
    SeedSessionRequest,
    StartSessionRequest,
)

logger = logging.getLogger("daiv.sandbox")


class DAIVSandboxClient:
    """
    Client to interact with the daiv-sandbox service.
    """

    def __init__(self):
        self.url = settings.SANDBOX_URL.unicode_string()

    async def start_session(self, request: StartSessionRequest) -> str:
        """
        Start a session with the sandbox.

        Args:
            request (StartSessionRequest): The request to start the session.

        Returns:
            The session ID.
        """
        async with httpx.AsyncClient(
            timeout=site_settings.sandbox_timeout, base_url=self.url, headers=self._get_headers()
        ) as client:
            response = await client.post("session/", json=request.model_dump(mode="json"))
            response.raise_for_status()
            return response.json()["session_id"]

    async def seed_session(self, session_id: str, repo_archive: bytes) -> None:
        """
        Seed a session with the initial state of /repo.

        One-shot per session. A 409 from the sandbox (already seeded) is
        treated as a no-op so retries and checkpoint replays are safe.
        """
        async with httpx.AsyncClient(
            timeout=site_settings.sandbox_timeout, base_url=self.url, headers=self._get_headers()
        ) as client:
            response = await client.post(
                f"session/{session_id}/seed/",
                json=SeedSessionRequest(repo_archive=base64.b64encode(repo_archive)).model_dump(mode="json"),
            )
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
        async with httpx.AsyncClient(
            timeout=site_settings.sandbox_timeout, base_url=self.url, headers=self._get_headers()
        ) as client:
            response = await client.post(f"session/{session_id}/files/", json=request.model_dump(mode="json"))
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
        async with httpx.AsyncClient(
            timeout=site_settings.sandbox_timeout, base_url=self.url, headers=self._get_headers()
        ) as client:
            response = await client.post(f"session/{session_id}/", json=request.model_dump(mode="json"))
            response.raise_for_status()
            return RunCommandsResponse.model_validate(response.json())

    async def close_session(self, session_id: str):
        """
        Close a session with the sandbox.

        Args:
            session_id (str): The session ID.
        """
        async with httpx.AsyncClient(
            timeout=site_settings.sandbox_timeout, base_url=self.url, headers=self._get_headers()
        ) as client:
            response = await client.delete(f"session/{session_id}/")
            response.raise_for_status()

    def _get_headers(self) -> dict[str, str]:
        """
        Get the headers for the request.
        """
        api_key = site_settings.sandbox_api_key
        if api_key is None:
            return {}
        return {"X-API-KEY": api_key.get_secret_value()}
