import httpx

from core.conf import settings

from .schemas import RunCommandsRequest, RunCommandsResponse, StartSessionRequest


class DAIVSandboxClient:
    """
    Client to interact with the daiv-sandbox service.
    """

    def __init__(self):
        self.url = settings.SANDBOX_URL.unicode_string()
        self.api_key = settings.SANDBOX_API_KEY and settings.SANDBOX_API_KEY.get_secret_value()

    async def start_session(self, request: StartSessionRequest) -> str:
        """
        Start a session with the sandbox.

        Args:
            request (StartSessionRequest): The request to start the session.

        Returns:
            The session ID.
        """
        async with httpx.AsyncClient(
            timeout=settings.SANDBOX_TIMEOUT, base_url=self.url, headers=self._get_headers()
        ) as client:
            response = await client.post("session/", json=request.model_dump(mode="json"))
            response.raise_for_status()
            return response.json()["session_id"]

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
            timeout=settings.SANDBOX_TIMEOUT, base_url=self.url, headers=self._get_headers()
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
            timeout=settings.SANDBOX_TIMEOUT, base_url=self.url, headers=self._get_headers()
        ) as client:
            response = await client.delete(f"session/{session_id}/")
            response.raise_for_status()

    def _get_headers(self) -> dict[str, str]:
        """
        Get the headers for the request.
        """
        if self.api_key is None:
            return {}
        return {"X-API-KEY": self.api_key}
