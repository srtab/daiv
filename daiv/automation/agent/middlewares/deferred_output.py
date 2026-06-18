from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

from codebase.context import RuntimeCtx  # noqa: TC001

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from langgraph.runtime import Runtime

logger = logging.getLogger("daiv.agent")

_DIGEST_LEN = 12


class DeferredOutputMiddleware(AgentMiddleware[AgentState[Any], RuntimeCtx]):
    """Defer a subagent's final output to a file on the workspace filesystem.

    When on a subagent's middleware stack, ``aafter_agent`` writes the subagent's output — its
    ``structured_response`` serialized as JSON, else its last message's text — to
    ``<output_dir>/<name>-<sha256[:12]>.<ext>`` via the backend, then clears ``structured_response``
    and appends a one-line pointer message. deepagents builds the ``task`` ToolMessage from that
    pointer (``_return_command_with_state_update`` falls to its last-message branch once
    ``structured_response`` is ``None``), so the orchestrator gets a path instead of the payload and
    never transcribes it back out.

    The write goes through ``backend.awrite`` (not a ``write_file`` tool), so a read-only detector
    emits a file without gaining any write tool. On any write failure the hook returns ``None`` (no
    state update): ``structured_response`` survives and deepagents inlines it exactly as before — a
    write error degrades to the old behaviour, never drops findings.
    """

    def __init__(self, *, backend: BackendProtocol, name: str, output_dir: str) -> None:
        self._backend = backend
        self._name = name
        self._output_dir = output_dir.rstrip("/")

    async def aafter_agent(self, state: AgentState[Any], runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:  # noqa: ARG002
        try:
            payload, ext = self._extract(state)
        except Exception:
            logger.exception(
                "DeferredOutputMiddleware: failed to serialize output for %s; keeping inline output", self._name
            )
            return None
        if payload is None:
            logger.debug("DeferredOutputMiddleware: nothing to defer for %s", self._name)
            return None

        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
        path = f"{self._output_dir}/{self._name}-{digest}{ext}"

        try:
            result = await self._backend.awrite(path, payload)
        except Exception:
            logger.exception("DeferredOutputMiddleware: write to %s raised; keeping inline output", path)
            return None

        # The backend's write is create-only and rejects an existing path; with a content-hash
        # filename an existing path means our exact bytes are already there, so treat it as success.
        if result.error and "already exists" not in result.error.lower():
            logger.warning(
                "DeferredOutputMiddleware: write to %s failed (%s); keeping inline output", path, result.error
            )
            return None

        pointer = f"Output deferred to a file to keep it out of context. Read it when you need the contents: {path}"
        return {"structured_response": None, "messages": [AIMessage(content=pointer)]}

    def _extract(self, state: AgentState[Any]) -> tuple[str | None, str]:
        structured = state.get("structured_response")
        if structured is not None:
            return self._serialize(structured), ".json"
        messages = state.get("messages") or []
        if messages and (text := messages[-1].text):
            return text, ".txt"
        return None, ".txt"

    @staticmethod
    def _serialize(structured: Any) -> str:
        # Mirror deepagents' serialization for dict and pydantic responses so the file matches what
        # would have been inlined (subagents.py _return_command_with_state_update). Detector
        # response_format is a JSON-schema dict, so this takes the json.dumps branch.
        if hasattr(structured, "model_dump_json"):
            return structured.model_dump_json()
        return json.dumps(structured)
