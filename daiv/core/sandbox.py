from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from pydantic import Base64Bytes, BaseModel, Field, field_validator

from .conf import settings

MAX_OUTPUT_LENGTH = 10000


class StartSessionRequest(BaseModel):
    base_image: str | None = Field(default=None, description="The base image to start the session with.")
    dockerfile: str | None = Field(default=None, description="The Dockerfile to use to build the base image.")

    @classmethod
    @field_validator("base_image", "dockerfile")
    def validate_base_image_or_dockerfile(cls, v, values):
        if not v and not values.get("dockerfile"):
            raise ValueError("Either base_image or dockerfile must be provided. Both cannot be None.")
        return v


class RunCommandsRequest(BaseModel):
    commands: list[str] = Field(description="The commands to run in the session.")
    workdir: str | None = Field(default=None, description="The working directory to use for the commands.")
    archive: Base64Bytes = Field(description="The archive to use as the working directory for the commands.")
    extract_changed_files: bool = Field(
        default=True, description="Whether to extract the changed files by the commands."
    )
    fail_fast: bool = Field(default=True, description="Whether to fail fast if any command fails.")


class RunCommandResult(BaseModel):
    """
    The result of running a command in the sandbox.
    """

    command: str
    output: str = Field(description="The output of the command. Truncated to 10000 characters.")
    exit_code: int

    @field_validator("output")
    @classmethod
    def validate_output(cls, v):
        return v[:MAX_OUTPUT_LENGTH]


class RunCommandResponse(BaseModel):
    """
    The response from running commands in the sandbox.
    """

    results: list[RunCommandResult]
    archive: Base64Bytes | None


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

    async def run_commands(self, session_id: str, request: RunCommandsRequest) -> RunCommandResponse:
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
            return RunCommandResponse.model_validate(response.json())

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

    @classmethod
    @asynccontextmanager
    async def session(cls, request: StartSessionRequest) -> AsyncGenerator[str]:
        """
        Context manager to start and close a session with the sandbox.
        """
        client = cls()
        session_id = await client.start_session(request)
        yield session_id
        await client.close_session(session_id)
