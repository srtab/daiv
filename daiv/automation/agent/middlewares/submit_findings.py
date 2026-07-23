from __future__ import annotations

import logging

import jsonschema
from langchain_core.tools import StructuredTool

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
