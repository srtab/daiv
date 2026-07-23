from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import jsonschema
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool

from automation.agent.middlewares.reminders import append_system_reminder

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult
    from langchain_core.messages import AnyMessage

logger = logging.getLogger("daiv.agent")

SUBMIT_FINDINGS_TOOL_NAME = "submit_findings"

# Success sentinel prefix on the tool's result. The enforcer middleware and
# DeferredOutputMiddleware both key on it to tell a recorded submission apart from a
# validation-failed attempt.
SUBMITTED_MARKER = "Findings recorded"

SUBMIT_FINDINGS_DESCRIPTION = (
    "Record your final findings. Call exactly once, when your audit is complete: pass every "
    'finding you are reporting as {"findings": [...]} — pass an empty list when you found no '
    "qualifying issues. This is the ONLY way findings are recorded; findings left in prose are "
    "discarded. After the tool confirms, finish with a one-line text summary."
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
    "You attempted to finish without calling `submit_findings`. Findings are recorded ONLY "
    'through that tool — prose is discarded. Call `submit_findings` now with {"findings": [...]}; '
    "pass an empty list if you found no qualifying issues."
    "</system-reminder>"
)


def _has_successful_submit(messages: list[AnyMessage]) -> bool:
    return any(
        isinstance(message, ToolMessage)
        and message.name == SUBMIT_FINDINGS_TOOL_NAME
        and isinstance(message.content, str)
        and message.content.startswith(SUBMITTED_MARKER)
        for message in messages
    )


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
