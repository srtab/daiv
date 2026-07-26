from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import jsonschema
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command

from automation.agent.middlewares.reminders import append_system_reminder

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult, ToolCallRequest
    from langchain_core.messages import AnyMessage

logger = logging.getLogger("daiv.agent")

SUBMIT_FINDINGS_TOOL_NAME = "submit_findings"

SUBMIT_FINDINGS_DESCRIPTION = (
    "Record the final audit result. Call exactly once after every inspection and reasoning step is "
    "complete. This must be the only tool call in the response and the final tool call of the run: a "
    "successful call ends the run immediately, so whatever you have not inspected by then is never "
    'inspected. Pass every qualifying finding as {"findings": [...]}, or an empty list when the audit '
    "is clean. Findings left in prose are discarded."
)

_VALIDATION_MESSAGE_LIMIT = 500


def build_submit_findings_tool(findings_schema: dict) -> StructuredTool:
    """Build the detector's terminal ``submit_findings`` tool from the findings object schema.

    ``findings_schema`` is the ``{"findings": [...]}`` object schema (see
    ``_load_detector_findings_schema``). It is advertised verbatim as the tool's args schema —
    the same shape the model previously saw as the forced structured-output tool — and
    re-validated handler-side with ``jsonschema`` because langchain does not validate dict
    args schemas.

    This is a ``content_and_artifact`` tool: a successful call returns the validated payload as
    the ``ToolMessage.artifact``, and that artifact is both the success signal and the payload.
    ``SubmitFindingsEnforcerMiddleware`` promotes it into ``structured_response`` and ends the
    run there. A validation failure returns the error as content with **no** artifact, so it
    reads as "nothing recorded" everywhere and the model can correct and retry — the retry loop
    is why the terminus cannot be the tool's own ``return_direct``, which would fire on a failed
    call too.
    """

    def _rejected(detail: str) -> tuple[str, None]:
        """A retryable refusal: the `Validation failed` framing the model keys on, and no artifact.

        The absent artifact is what keeps the run alive — see ``_promote_submission``.
        """
        return f"Validation failed: {detail} Fix the payload and call {SUBMIT_FINDINGS_TOOL_NAME} again.", None

    def _submit(findings: list) -> tuple[str, dict | None]:
        try:
            jsonschema.validate({"findings": findings}, findings_schema)
        except jsonschema.ValidationError as exc:
            logger.info("submit_findings: payload failed schema validation: %s", exc.message[:200])
            return _rejected(f"{exc.message[:_VALIDATION_MESSAGE_LIMIT]}.")
        # `findings.py merge` drops a custom-rules finding that cites no `source`, but the schema
        # cannot require it conditionally and stay the provider-acceptable subset the tool
        # advertises. Without this check the model is told "recorded", the payload exports as a
        # success-signalling .json, and merge discards it with no chance to correct.
        if sourceless := [f for f in findings if f.get("detector") == "custom-rules" and not f.get("source")]:
            logger.info("submit_findings: %d custom-rules finding(s) missing `source`.", len(sourceless))
            return _rejected(
                f"{len(sourceless)} custom-rules finding(s) have no `source`. Every custom-rules "
                "finding must cite the rule it enforces as `<original-path>:<line> — <concise rule>`."
            )
        logger.info("submit_findings: recorded %d finding(s).", len(findings))
        # The model never reads this content — the run ends before another model call — so it is
        # written for the transcript and for whoever is debugging a detector, not as an instruction.
        return f"Findings recorded ({len(findings)} finding(s)); the audit is complete and the run ends here.", {
            "findings": findings
        }

    return StructuredTool.from_function(
        func=_submit,
        name=SUBMIT_FINDINGS_TOOL_NAME,
        description=SUBMIT_FINDINGS_DESCRIPTION,
        args_schema=findings_schema,
        response_format="content_and_artifact",
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
# tool call. It is built here rather than by the tool, so it carries no artifact and can never be
# promoted into `structured_response`: a rejected batch reads as "nothing recorded" everywhere and
# stays retryable — exactly like a schema-validation failure.
BATCHED_SUBMIT_REJECTION = (
    "Not recorded: `submit_findings` was called alongside {siblings} other tool call(s) in the same "
    "response, so nothing was submitted. If any inspection or reasoning remains, continue the audit "
    "first with the read-only tools you need. When the audit is complete, call `submit_findings` "
    "again as the ONLY tool call in that response, passing every finding (or an empty list when the "
    "audit is clean)."
)


def _state_get(state: Any, key: str) -> Any:
    """Read a key out of an agent state that may be a dict or a pydantic model."""
    if isinstance(state, dict):
        return state.get(key)
    return getattr(state, key, None)


def _recorded_findings(state: Any) -> Any:
    """The promoted ``submit_findings`` payload, or ``None`` if nothing was recorded.

    The single definition of "this detector recorded findings". It is a state read rather than a
    scan of message history for an acknowledgement string: only ``_promote_submission`` writes
    this channel, and only for a call that returned an artifact, so a validation-failed or
    batch-rejected attempt cannot be mistaken for a submission by a prefix that happens to match.
    """
    return _state_get(state, "structured_response")


def _promote_submission(result: ToolMessage | Command[Any]) -> ToolMessage | Command[Any]:
    """Move a successful submission's payload out of the tool result and into agent state.

    The tool cannot do this itself. Injected parameters (``ToolRuntime``, ``InjectedToolCallId``)
    are resolved from the tool's args schema, and ``submit_findings`` advertises a raw JSON-Schema
    dict so the finding shape reaches the model verbatim — nothing is injected into a dict-schema
    tool, so the tool never learns its own ``tool_call_id`` and cannot build the ``Command``. This
    middleware already holds it.

    ``structured_response`` is reused deliberately instead of a bespoke ``state_schema`` key, which
    is otherwise this package's convention. Two reasons, and the second is the load-bearing one:

    * langgraph silently DROPS a ``Command.update`` key that is not a declared channel (it filters
      updates to the state schema), so a bespoke key that is ever mis-declared fails as a no-op
      rather than an error — the detector would simply look empty.
    * ``structured_response`` is the channel **deepagents itself** falls back to when building the
      ``task`` result (``_return_command_with_state_update``). That is what lets
      ``DeferredOutputMiddleware`` degrade a failed file write by returning ``None`` and nothing
      else: the payload is still inlined by deepagents. On a bespoke key a write failure would lose
      the findings outright, which is exactly the rescue path that channel already provides.

    The cost of the reuse is that ``structured_response`` carries two meanings in a detector graph,
    so detectors must stay compiled with ``response_format=None`` (``subagents.py``) — otherwise a
    model-produced structured output would be indistinguishable from a recorded submission.
    """
    if not isinstance(result, ToolMessage) or result.artifact is None:
        return result
    return Command(update={"structured_response": result.artifact, "messages": [result]})


def _issuing_message(messages: list[AnyMessage], tool_call_id: str) -> AIMessage | None:
    """The ``AIMessage`` whose tool_calls contain ``tool_call_id``, or ``None``.

    ``tool_call_id`` is non-optional by contract: matching on ``None`` would pair this call with
    any other id-less tool call. The caller filters that case out before calling.
    """
    for message in reversed(messages):
        if isinstance(message, AIMessage) and any(
            tool_call["id"] == tool_call_id for tool_call in message.tool_calls or []
        ):
            return message
    return None


class SubmitFindingsEnforcerMiddleware(AgentMiddleware):
    """Make ``submit_findings`` a detector's terminus — reached, and not survived.

    Detectors are not forced into structured output (``tool_choice="any"``), so the model can
    think in text and stop naturally — but nothing intrinsically makes it record findings before
    stopping, nor stop after recording. Three holes, closed in two hooks:

    * Model tries to finish (no tool calls) WITHOUT a recorded submission → retry within the node
      with an ephemeral nudge, up to ``MAX_FINALIZE_NUDGES`` times; if it still refuses, let the
      text response through — the run ends, ``DeferredOutputMiddleware`` defers it as ``.txt``,
      and the orchestrator counts the detector as failed, never as "no findings".
    * Submission recorded → its payload is promoted into ``structured_response``
      (``_promote_submission``) and the NEXT model call is short-circuited into a final text
      message. The model is never called again, so it cannot resume inspecting after recording
      (the pattern-locked-detector failure mode), and the detector costs one fewer full-context
      model call than it would to merely say "done".
    * Model batches ``submit_findings`` with another tool call → the submission is refused in
      ``awrap_tool_call`` with a corrective tool result; nothing is recorded and the model retries.

    The terminus is a short-circuit rather than a graph jump on purpose. A tool-level
    ``Command(goto=END)`` is a silent no-op — langgraph drops ``END`` gotos, and the tools→model
    edge fires regardless — and ``return_direct=True`` is static, so it would also end the run on
    a *failed* validation and destroy the correct-and-retry loop. Returning a final ``AIMessage``
    without calling the model exits through the ordinary model→exit edge, which still runs the
    ``after_agent`` chain where ``DeferredOutputMiddleware`` writes the output file.

    Must sit LATER in the middleware list than ``LoopBreakerMiddleware`` (i.e. inside it): the
    breaker's terminal "ERROR: stopped after…" response is tool-call-free and unsubmitted, and an
    outer enforcer would nudge-retry it back to life instead of letting the run die.
    """

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        # Terminus. Checked BEFORE the handler: a recorded submission means there is nothing left
        # to ask the model, so the call is not made at all rather than made and then discarded.
        if _recorded_findings(request.state) is not None:
            logger.info("SubmitFindingsEnforcer: findings recorded; ending the run without another model call.")
            return AIMessage(content="Findings recorded; run complete.")

        response = await handler(request)

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
        """Record a successful submission into state, and refuse one batched with another call.

        Submission must be the sole tool call in its response and the last of the run.
        Enforcing that by rewriting the model response is not safe — dropping a ``tool_call`` from
        an ``AIMessage`` leaves the provider's ``tool_use`` content block without a matching
        ``tool_result`` and the next request is rejected. So the batch executes as issued and the
        refusal happens here instead: the sibling calls run normally, the submit handler does not,
        and the model gets a corrective tool result telling it what to do. A refusal is built here
        and so carries no artifact, meaning a batch can never be promoted into a *successful*
        submission — which is what makes "no inspection tool in the same batch as the successful
        submission" hold unconditionally.

        Fails open on a bookkeeping miss: if the issuing ``AIMessage`` is not in state (trimmed or
        summarized away), the call executes rather than silently losing a real submission.
        """
        if request.tool_call["name"] != SUBMIT_FINDINGS_TOOL_NAME:
            return await handler(request)

        # Upstream types `ToolCall.id` as optional. A `None` id cannot be matched against history
        # (it would match any other id-less call) and cannot be echoed back — `ToolMessage`
        # requires a `str`, so constructing a rejection would raise inside the middleware. Fail
        # open explicitly instead of crashing.
        tool_call_id = request.tool_call.get("id")
        issuing = _issuing_message(_state_get(request.state, "messages") or [], tool_call_id) if tool_call_id else None

        if tool_call_id is None:
            logger.warning("SubmitFindingsEnforcer: submit_findings call has no id; batch guard skipped.")
        elif issuing is None:
            # Distinct from "found it, and it was a sole call": the guard could not evaluate at
            # all (history trimmed/summarized, or an unexpected state shape). Same fail-open
            # behaviour, but it must not look like a verified-clean submission in the logs.
            logger.warning(
                "SubmitFindingsEnforcer: issuing AIMessage for tool_call %s not in state; "
                "batch guard skipped (failing open).",
                tool_call_id,
            )
        # `_issuing_message` matched this id against `tool_calls`, so the list is non-empty.
        elif (siblings := len(issuing.tool_calls) - 1) > 0:
            logger.warning(
                "SubmitFindingsEnforcer: submit_findings batched with %d other tool call(s); not recording.", siblings
            )
            return ToolMessage(
                content=BATCHED_SUBMIT_REJECTION.format(siblings=siblings),
                name=SUBMIT_FINDINGS_TOOL_NAME,
                tool_call_id=tool_call_id,
            )

        # Single promotion point: every path that actually runs the handler records through here, so
        # a branch added above cannot half-implement the protocol and silently drop findings.
        return _promote_submission(await handler(request))
