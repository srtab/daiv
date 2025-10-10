from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from codebase.signals import before_reset_repository_ctx

from .client import DAIVSandboxClient

if TYPE_CHECKING:
    from .schemas import RunCommandsRequest, RunCommandsResponse, StartSessionRequest


@dataclass(frozen=True)
class SandboxCtx:
    """
    Context to be used to temporarily store the session id that is used to run the bash commands in the sandbox.
    """

    session_id: str
    """The sandbox session identifier"""


sandbox_ctx: ContextVar[SandboxCtx | None] = ContextVar[SandboxCtx | None]("sandbox_ctx", default=None)


async def start_sandbox_session(request: StartSessionRequest):
    """
    Start a sandbox session.

    Args:
        request: The request to start the sandbox session.

    Returns:
        The result of the commands.
    """
    if sandbox_ctx.get() is not None:
        return

    session_id = await DAIVSandboxClient().start_session(request)
    sandbox_ctx.set(SandboxCtx(session_id=session_id))


async def run_sandbox_commands(request: RunCommandsRequest) -> RunCommandsResponse:
    """
    Run commands in the sandbox session.

    Args:
        request: The request to run the commands.

    Returns:
        The result of the commands.

    Raises:
        RuntimeError: If no sandbox session id is found.
    """
    if (context := sandbox_ctx.get()) is None:
        raise RuntimeError(
            "No sandbox session id found. Please start a sandbox session first with `start_sandbox_session`."
        )

    return await DAIVSandboxClient().run_commands(context.session_id, request)


async def close_sandbox_session(**kwargs):
    """
    Close the sandbox session.
    """
    if sandbox_ctx.get() is None:
        return

    await DAIVSandboxClient().close_session(sandbox_ctx.get().session_id)


before_reset_repository_ctx.connect(
    close_sandbox_session, sender="set_repository_ctx", dispatch_uid="close_sandbox_session"
)
