from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import AgentState
from langchain.tools import ToolRuntime

from automation.agent.utils import get_context_file_content
from codebase.context import RuntimeCtx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deepagents.backends.protocol import BACKEND_TYPES
    from langgraph.runtime import Runtime


LONG_TERM_MEMORY_SYSTEM_PROMPT = """\
## Long-term Memory

The project root contains a file called AGENTS.md that works as your long-term memory and persists across sessions. It serves multiple purposes:

  1. Storing frequently used bash commands (build, test, lint, etc.) so you can use them without searching each time
  2. Recording the project's code style preferences (naming conventions, preferred libraries, etc.)
  3. Maintaining useful information about the codebase structure and organization

<memory>
{memory}
</memory>"""  # noqa: E501


class LongTermMemoryState(AgentState):
    """
    Schema for the long-term memory state.
    """

    memory: str


class LongTermMemoryMiddleware(AgentMiddleware):
    """
    Middleware to inject the long-term memory from the AGENTS.md file into the agent state.

    Example:
        ```python
        from langchain.agents import create_agent

        agent = create_agent(
            model="openai:gpt-4o",
            middleware=[LongTermMemoryMiddleware()],
            context_schema=RuntimeCtx,
        )
        ```
    """

    state_schema = LongTermMemoryState

    def __init__(self, *, backend: BACKEND_TYPES) -> None:
        """
        Initialize the long-term memory middleware.

        Args:
            backend: The backend to use for the long-term memory.
        """
        self.backend = backend

    async def abefore_agent(self, state: AgentState, runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:
        """
        Before the agent starts, inject the long-term memory from the AGENTS.md file into the agent state.

        Args:
            state (AgentState): The state of the agent.
            runtime (Runtime[RuntimeCtx]): The runtime context containing the repository id.

        Returns:
            dict[str, Any] | None: The state updates with the long-term memory from the AGENTS.md file.
        """
        context_file_content = await get_context_file_content(
            Path(runtime.context.repo.working_dir),
            runtime.context.config.context_file_name,
            backend=self.backend(
                # Need to manually create the runtime object since the ToolRuntime object is not available in the
                # before_agent method.
                runtime=ToolRuntime[RuntimeCtx, AgentState](
                    state=state,
                    context=runtime.context,
                    config={},
                    stream_writer=runtime.stream_writer,
                    tool_call_id=None,
                    store=runtime.store,
                )
            ),
        )

        if not context_file_content:
            return None

        return {"memory": context_file_content}

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Update the system prompt with the long-term memory system prompt.

        Args:
            request: The model request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The model response from the handler.
        """
        if memory := request.state.get("memory"):
            system_prompt = LONG_TERM_MEMORY_SYSTEM_PROMPT.format(memory=memory)
            request = request.override(system_prompt=request.system_prompt + "\n\n" + system_prompt)

        return await handler(request)
