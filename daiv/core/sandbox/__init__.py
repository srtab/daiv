from typing import TYPE_CHECKING

from codebase.signals import before_reset_repository_ctx

from .client import DAIVSandboxClient

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

    from .schemas import RunCommandsRequest, RunCommandsResponse, StartSessionRequest


async def start_sandbox_session(request: StartSessionRequest, store: BaseStore):
    """
    Start a sandbox session or reuse existing one from store.

    Args:
        request: The request to start the sandbox session.
        store: The store to use for persisting session ID.

    Returns:
        The result of the commands.
    """
    from automation.utils import get_sandbox_session, register_sandbox_session

    # Check if session exists in store (persisted across tool calls)
    if await get_sandbox_session(store):
        return

    # Create new session
    session_id = await DAIVSandboxClient().start_session(request)

    # Save to store for reuse across tool calls
    await register_sandbox_session(store, session_id)


async def run_sandbox_commands(request: RunCommandsRequest, store: BaseStore) -> RunCommandsResponse:
    """
    Run commands in the sandbox session.

    Args:
        request: The request to run the commands.
        store: The store to retrieve the session ID from.

    Returns:
        The result of the commands.

    Raises:
        RuntimeError: If no sandbox session id is found.
    """
    from automation.utils import get_sandbox_session

    if (session_id := await get_sandbox_session(store)) is None:
        raise RuntimeError(
            "No sandbox session id found. Please start a sandbox session first with `start_sandbox_session`."
        )

    return await DAIVSandboxClient().run_commands(session_id, request)


async def close_sandbox_session(store: BaseStore | None = None, **kwargs):
    """
    Close the sandbox session.

    Args:
        store: The store to retrieve and clean up the session ID. If None, session won't be closed.
    """
    if store is None:
        return

    from automation.utils import delete_sandbox_session, get_sandbox_session

    if (session_id := await get_sandbox_session(store)) is None:
        return

    await DAIVSandboxClient().close_session(session_id)
    await delete_sandbox_session(store)


before_reset_repository_ctx.connect(
    close_sandbox_session, sender="set_repository_ctx", dispatch_uid="close_sandbox_session"
)
