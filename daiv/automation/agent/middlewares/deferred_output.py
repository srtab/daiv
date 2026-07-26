from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

from codebase.context import RuntimeCtx  # noqa: TC001

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from langgraph.runtime import Runtime

logger = logging.getLogger("daiv.agent")

_DIGEST_LEN = 12

# The extension reflects the payload's shape, and only that: `.json` for a structured payload,
# `.txt` for final text. Naming the two keeps a third from being introduced by a typo. Consumers
# are free to read meaning into the split — code-review's `findings.py merge` treats `.txt` as a
# failed detector — but that interpretation lives with the consumer, not here.
DeferredKind = Literal[".json", ".txt"]


class _Deferred(NamedTuple):
    """What to write, and which of the two downstream states it signals."""

    payload: str
    ext: DeferredKind


class DeferredOutputMiddleware(AgentMiddleware[AgentState[Any], RuntimeCtx]):
    """Defer a subagent's final output to a file on the workspace filesystem.

    When on a subagent's middleware stack, ``aafter_agent`` writes the subagent's output — its
    ``structured_response`` serialized as JSON, else its last non-empty ``AIMessage`` text, else a
    failure sentinel — to ``<output_dir>/<name>-<sha256[:12]>.<ext>`` via the backend, then clears
    ``structured_response`` and appends a one-line pointer message. deepagents
    builds the ``task`` ToolMessage from that pointer (``_return_command_with_state_update`` falls to
    its last-message branch once ``structured_response`` is ``None``), so the orchestrator gets a
    path instead of the payload and never transcribes it back out.

    This middleware is deliberately generic: it knows nothing about detectors or
    ``submit_findings``. A detector's recorded findings arrive here as ``structured_response``
    because ``SubmitFindingsEnforcerMiddleware`` promotes the submission into that channel, so
    there is one payload path, not one per producer.

    The write goes through ``backend.awrite`` (not a ``write_file`` tool), so a read-only detector
    emits a file without gaining any write tool. A write failure degrades rather than dropping
    output: returning ``None`` leaves ``structured_response`` untouched and deepagents inlines it
    (verbose, but intact), and a text payload is what deepagents would inline anyway.
    """

    def __init__(self, *, backend: BackendProtocol, name: str, output_dir: str) -> None:
        self._backend = backend
        self._name = name
        self._output_dir = output_dir.rstrip("/")

    async def aafter_agent(self, state: AgentState[Any], runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:  # noqa: ARG002
        try:
            extracted = self._extract(state)
        except Exception:
            logger.exception(
                "DeferredOutputMiddleware: failed to serialize output for %s; keeping inline output", self._name
            )
            return None
        if extracted is None:
            logger.debug("DeferredOutputMiddleware: nothing to defer for %s", self._name)
            return None

        digest = hashlib.sha256(extracted.payload.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
        path = f"{self._output_dir}/{self._name}-{digest}{extracted.ext}"

        try:
            result = await self._backend.awrite(path, extracted.payload)
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

    def _extract(self, state: AgentState[Any]) -> _Deferred | None:
        structured = state.get("structured_response")
        if structured is not None:
            return _Deferred(self._serialize(structured), ".json")
        messages = state.get("messages") or []
        if not messages:
            return None
        # Walk back rather than reading messages[-1]: Anthropic occasionally emits a trailing
        # empty `end_turn` AIMessage after a final tool call (deepagents' own result builder
        # skips it for the same reason). Reading the literal last message would yield no text,
        # defer nothing at all, and make a failed detector vanish from the fan-out entirely —
        # indistinguishable downstream from a detector that was never dispatched.
        for message in reversed(messages):
            if isinstance(message, AIMessage) and (text := (message.text or "").strip()):
                return _Deferred(text, ".txt")
        # A subagent that produced messages but no extractable output still has to leave a file
        # behind: deferring nothing at all is indistinguishable from never having been dispatched,
        # whereas a `.txt` is something the caller can count.
        logger.warning(
            "DeferredOutputMiddleware: %s produced no extractable output; deferring a failure sentinel", self._name
        )
        sentinel = f"{self._name} ended with no structured output and no final text."
        return _Deferred(sentinel, ".txt")

    @staticmethod
    def _serialize(structured: Any) -> str:
        # Mirror deepagents' serialization for dict and pydantic responses so the file matches what
        # would have been inlined (subagents.py _return_command_with_state_update). This is the
        # general structured_response serialization path: pydantic models use model_dump_json,
        # everything else (dicts, etc.) falls through to json.dumps.
        if hasattr(structured, "model_dump_json"):
            return structured.model_dump_json()
        return json.dumps(structured)
