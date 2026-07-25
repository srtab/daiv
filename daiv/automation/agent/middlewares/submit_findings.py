from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import jsonschema
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool

from automation.agent.middlewares.reminders import append_system_reminder

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult, ToolCallRequest
    from langchain_core.messages import AnyMessage
    from langgraph.types import Command

logger = logging.getLogger("daiv.agent")

SUBMIT_FINDINGS_TOOL_NAME = "submit_findings"

# Success sentinel prefix on the tool's result. The enforcer middleware and
# DeferredOutputMiddleware both key on it to tell a recorded submission apart from a
# validation-failed attempt.
SUBMITTED_MARKER = "Findings recorded"

SUBMIT_FINDINGS_DESCRIPTION = (
    "Record the final audit result. Call exactly once after every inspection and reasoning step is "
    "complete. This must be the only tool call in the response and the final tool call of the run. "
    'Pass every qualifying finding as {"findings": [...]}, or an empty list when the audit is clean. '
    "Findings left in prose are discarded. After confirmation, return only a one-line acknowledgement "
    "and do not call another tool."
)

_VALIDATION_MESSAGE_LIMIT = 500


def build_submit_findings_tool(findings_schema: dict) -> StructuredTool:
    """Build the detector's terminal ``submit_findings`` tool from the findings object schema.

    ``findings_schema`` is the ``{"findings": [...]}`` object schema (see
    ``_load_detector_findings_schema``). It is advertised verbatim as the tool's args schema —
    the same shape the model previously saw as the forced structured-output tool — and
    re-validated handler-side with ``jsonschema`` because langchain does not validate dict
    args schemas. On validation failure the tool returns the error as its result so the model
    can correct and retry. On success it acknowledges with ``SUBMITTED_MARKER``; the recorded
    payload deliberately lives nowhere but the tool-call args already in message history —
    ``DeferredOutputMiddleware`` extracts it from there at run end, so no state plumbing.
    """

    def _submit(findings: list) -> str:
        try:
            jsonschema.validate({"findings": findings}, findings_schema)
        except jsonschema.ValidationError as exc:
            logger.info("submit_findings: payload failed schema validation: %s", exc.message[:200])
            return (
                f"Validation failed: {exc.message[:_VALIDATION_MESSAGE_LIMIT]}. "
                f"Fix the payload and call {SUBMIT_FINDINGS_TOOL_NAME} again."
            )
        logger.info("submit_findings: recorded %d finding(s).", len(findings))
        return (
            f"{SUBMITTED_MARKER} ({len(findings)} finding(s)). "
            "You are done: respond with a one-line text summary to finish the run."
        )

    return StructuredTool.from_function(
        func=_submit,
        name=SUBMIT_FINDINGS_TOOL_NAME,
        description=SUBMIT_FINDINGS_DESCRIPTION,
        args_schema=findings_schema,
    )


MAX_FINALIZE_NUDGES = 2

FINALIZE_NUDGE = (
    "<system-reminder>"
    "You attempted to finish without recording the audit result. If any inspection or reasoning "
    "remains, continue the audit first using the necessary read-only tools. Otherwise call "
    "`submit_findings` with every finding, or an empty list when clean. `submit_findings` must be "
    "the only tool call in that response and the final tool call of the run; prose findings are "
    "discarded."
    "</system-reminder>"
)

# Returned in place of the tool's result when the model batched `submit_findings` with any other
# tool call. Deliberately does NOT carry SUBMITTED_MARKER: `_has_successful_submit` and
# DeferredOutputMiddleware._submitted_payload both key on that marker, so a rejected batch reads as
# "nothing recorded" everywhere and stays retryable — exactly like a schema-validation failure.
BATCHED_SUBMIT_REJECTION = (
    "Not recorded: `submit_findings` was called alongside {siblings} other tool call(s) in the same "
    "response, so nothing was submitted. If any inspection or reasoning remains, continue the audit "
    "first with the read-only tools you need. When the audit is complete, call `submit_findings` "
    "again as the ONLY tool call in that response, passing every finding (or an empty list when the "
    "audit is clean)."
)


def _has_successful_submit(messages: list[AnyMessage]) -> bool:
    return any(
        isinstance(message, ToolMessage)
        and message.name == SUBMIT_FINDINGS_TOOL_NAME
        and isinstance(message.content, str)
        and message.content.startswith(SUBMITTED_MARKER)
        for message in messages
    )


def _state_messages(state: Any) -> list[AnyMessage]:
    """Messages out of an agent state that may be a dict or a pydantic model."""
    if isinstance(state, dict):
        return state.get("messages") or []
    return getattr(state, "messages", None) or []


def _issuing_message(messages: list[AnyMessage], tool_call_id: str) -> AIMessage | None:
    """The ``AIMessage`` whose tool_calls contain ``tool_call_id``, or ``None``."""
    for message in reversed(messages):
        if isinstance(message, AIMessage) and any(
            tool_call["id"] == tool_call_id for tool_call in message.tool_calls or []
        ):
            return message
    return None


class SubmitFindingsEnforcerMiddleware(AgentMiddleware):
    """Guarantee a detector run ends through ``submit_findings`` — in both directions.

    Detectors are no longer forced into structured output (``tool_choice="any"``), so the model
    can think in text and stop naturally — but nothing intrinsically makes it record findings
    before stopping, nor stop after recording. Both holes are closed inside ``awrap_model_call``
    (no extra graph node; retries cost zero supersteps):

    * Model tries to finish (no tool calls) WITHOUT a recorded submission → retry within the node
      with an ephemeral nudge, up to ``MAX_FINALIZE_NUDGES`` times; if it still refuses, let the
      text response through — the run ends, ``DeferredOutputMiddleware`` defers it as ``.txt``,
      and the orchestrator counts the detector as failed, never as "no findings".
    * Model keeps calling tools AFTER a recorded submission → replace the response with a final
      text message so the run ends. Submission is the detector's terminal act by contract;
      anything after it is a leak (e.g. a pattern-locked model resuming file reads).
    * Model batches ``submit_findings`` with another tool call → the submission is refused in
      ``awrap_tool_call`` with a corrective tool result; nothing is recorded and the model retries.

    Must sit LATER in the middleware list than ``LoopBreakerMiddleware`` (i.e. inside it): the
    breaker's terminal "ERROR: stopped after…" response is tool-call-free and unsubmitted, and an
    outer enforcer would nudge-retry it back to life instead of letting the run die.
    """

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        response = await handler(request)

        if _has_successful_submit(request.messages):
            if getattr(response.result[-1], "tool_calls", None):
                logger.warning("SubmitFindingsEnforcer: model kept calling tools after submit_findings; finalizing.")
                return AIMessage(content="Findings already submitted; run complete.")
            return response

        for attempt in range(1, MAX_FINALIZE_NUDGES + 1):
            if getattr(response.result[-1], "tool_calls", None):
                return response
            logger.info(
                "SubmitFindingsEnforcer: finish attempt without submit_findings; nudging (%d/%d).",
                attempt,
                MAX_FINALIZE_NUDGES,
            )
            response = await handler(append_system_reminder(request, FINALIZE_NUDGE))

        if not getattr(response.result[-1], "tool_calls", None):
            logger.warning(
                "SubmitFindingsEnforcer: model never called submit_findings after %d nudges; giving up.",
                MAX_FINALIZE_NUDGES,
            )
        return response

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]
    ) -> ToolMessage | Command[Any]:
        """Refuse a ``submit_findings`` call that was batched with any other tool call.

        Invariants 2-3 make submission the sole tool call in its response and the last of the run.
        Enforcing that by rewriting the model response is not safe — dropping a ``tool_call`` from
        an ``AIMessage`` leaves the provider's ``tool_use`` content block without a matching
        ``tool_result`` and the next request is rejected. So the batch executes as issued and the
        refusal happens here instead: the sibling calls run normally, the submit handler does not,
        and the model gets a corrective tool result telling it what to do. Since the reply lacks
        ``SUBMITTED_MARKER``, a batch can never be a *successful* submission — which is what makes
        "no inspection tool in the same batch as the successful submission" hold unconditionally.

        Fails open on a bookkeeping miss: if the issuing ``AIMessage`` is not in state (trimmed or
        summarized away), the call executes rather than silently losing a real submission.
        """
        if request.tool_call["name"] != SUBMIT_FINDINGS_TOOL_NAME:
            return await handler(request)

        issuing = _issuing_message(_state_messages(request.state), request.tool_call["id"])
        siblings = len(issuing.tool_calls or []) - 1 if issuing is not None else 0
        if siblings > 0:
            logger.warning(
                "SubmitFindingsEnforcer: submit_findings batched with %d other tool call(s); not recording.", siblings
            )
            return ToolMessage(
                content=BATCHED_SUBMIT_REJECTION.format(siblings=siblings),
                name=SUBMIT_FINDINGS_TOOL_NAME,
                tool_call_id=request.tool_call["id"],
            )
        return await handler(request)
