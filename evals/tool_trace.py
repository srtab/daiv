"""Opt-in eval diagnostics; no model requests, tracing service, or production middleware.

``DAIV_EVAL_TRACE_OUT`` enables one JSONL row per agent invocation. It retains tool arguments,
returned content, errors, and tool messages observed by the model-start callback. Parent run
links associate nested tools with their ``task(subagent_type=...)`` detector, including parallel
tasks. Repeated model-visible results are stored once per detector, tool-call ID, and content; repeated tool
invocations remain separate events. Injected runtime objects and non-tool model messages are omitted.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage
from langgraph.types import Command

if TYPE_CHECKING:
    from collections.abc import Generator


def _value(value):
    if isinstance(value, ToolMessage):
        return {
            "type": "tool",
            "name": value.name,
            "content": _value(value.content),
            "tool_call_id": value.tool_call_id,
            "status": value.status,
        }
    if isinstance(value, Command):
        update = value.update if isinstance(value.update, dict) else {}
        return {"type": "command", "messages": _value(update.get("messages", []))}
    if isinstance(value, dict):
        return {str(k): _value(v) for k, v in value.items() if k != "runtime"}
    if isinstance(value, list | tuple):
        return [_value(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return {"type": type(value).__name__}


class ToolTraceRecorder(BaseCallbackHandler):
    """A per-invocation callback shared by the parent and inherited subagent calls."""

    run_inline = True

    def __init__(self):
        self.events = []
        self._parents = {}
        self._tools = {}
        self._detectors = {}
        self._result_detectors = {}
        self._seen_model_results = set()
        self._lock = RLock()

    def _link(self, run_id, parent_run_id):
        self._parents[str(run_id)] = str(parent_run_id) if parent_run_id is not None else None

    def _detector(self, run_id):
        current = str(run_id)
        visited = set()
        while current is not None and current not in visited:
            visited.add(current)
            if current in self._detectors:
                return self._detectors[current]
            current = self._parents.get(current)
        return None

    def _event(self, event, run_id, tool_name, **fields):
        self.events.append({
            "event": event,
            "run_id": str(run_id),
            "parent_run_id": self._parents.get(str(run_id)),
            "tool": tool_name,
            "detector": self._detector(run_id),
            **fields,
        })

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        with self._lock:
            self._link(run_id, parent_run_id)

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, inputs=None, **kwargs):
        with self._lock:
            self._link(run_id, parent_run_id)
            name = (serialized or {}).get("name", "unknown")
            self._tools[str(run_id)] = name
            if inputs is None:
                try:
                    inputs = json.loads(input_str)
                except ValueError, TypeError:
                    inputs = input_str
            if name == "task" and isinstance(inputs, dict) and isinstance(inputs.get("subagent_type"), str):
                self._detectors[str(run_id)] = inputs["subagent_type"]
            self._event("tool_start", run_id, name, args=_value(inputs))

    def on_tool_end(self, output, *, run_id, **kwargs):
        with self._lock:
            messages = [output]
            if isinstance(output, Command) and isinstance(output.update, dict):
                messages = output.update.get("messages", [])
            for message in messages:
                if isinstance(message, ToolMessage):
                    self._result_detectors[message.tool_call_id] = self._detector(run_id)
            self._event("tool_end", run_id, self._tools.get(str(run_id), "unknown"), output=_value(output))

    def on_tool_error(self, error, *, run_id, **kwargs):
        with self._lock:
            self._event(
                "tool_error",
                run_id,
                self._tools.get(str(run_id), "unknown"),
                error={"type": type(error).__name__, "message": str(error)},
            )

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        with self._lock:
            self._link(run_id, parent_run_id)
            for batch in messages:
                for message in batch:
                    if not isinstance(message, ToolMessage):
                        continue
                    output = _value(message)
                    digest = hashlib.sha256(json.dumps(output, sort_keys=True).encode()).hexdigest()
                    detector = self._detector(run_id)
                    key = (detector, message.tool_call_id, digest)
                    if key in self._seen_model_results:
                        continue
                    self._seen_model_results.add(key)
                    self._event(
                        "model_tool_result",
                        run_id,
                        message.name,
                        output=output,
                        sha256=digest,
                        result_detector=detector or self._result_detectors.get(message.tool_call_id),
                    )


@contextmanager
def capture_tool_trace(*, nodeid: str, model: str) -> Generator[ToolTraceRecorder | None]:
    output = os.environ.get("DAIV_EVAL_TRACE_OUT")
    if not output:
        yield None
        return
    recorder = ToolTraceRecorder()
    error = None
    run = int(os.environ.get("DAIV_EVAL_RUN", "1"))
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Validate the destination before starting an agent that could incur provider charges.
    with path.open("a", encoding="utf-8") as handle:
        try:
            yield recorder
        except BaseException as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            row = {
                "schema_version": 1,
                "nodeid": nodeid,
                "run": run,
                "model": model,
                "events": recorder.events,
                "error": error,
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
