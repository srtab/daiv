from __future__ import annotations

import json
import logging
from typing import Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command

from .schemas import (
    FINALIZE_WITH_PLAN_DESCRIPTION,
    FINALIZE_WITH_TARGETED_QUESTIONS_DESCRIPTION,
    AskForClarification,
    ChangeInstructions,
    Plan,
)

logger = logging.getLogger("daiv.tools")


@tool("finalize_with_plan", args_schema=Plan, description=FINALIZE_WITH_PLAN_DESCRIPTION, return_direct=True)
def finalize_with_plan(changes: list[ChangeInstructions], tool_call_id: str) -> Command[Literal["plan_approval"]]:
    """
    Finalize the inspection with a self-contained plan.

    Args:
        changes (list[ChangeInstructions]): The plan to execute.

    Returns:
        Command[Literal["plan_approval"]]: The next step in the workflow.
    """  # noqa: E501
    logger.info("[finalize_with_plan] The plan to execute: %s", repr(changes))

    message = ToolMessage(content=json.dumps({"changes": changes}), tool_call_id=tool_call_id)

    return Command(goto="plan_approval", update={"plan_tasks": changes, "messages": [message]}, graph=Command.PARENT)


@tool(
    "finalize_with_targeted_questions",
    args_schema=AskForClarification,
    description=FINALIZE_WITH_TARGETED_QUESTIONS_DESCRIPTION,
    return_direct=True,
    response_format="content_and_artifact",
)
def finalize_with_targeted_questions(questions: str) -> tuple[str, dict]:
    """
    Finalize the inspection with targeted questions.

    Args:
        questions (str): The question(s) to ask the user for clarification.

    Returns:
        tuple[str, dict]: The question(s) to ask the user for clarification.
    """  # noqa: E501
    logger.info(
        "[finalize_with_targeted_questions] The question(s) to ask the user for clarification: %s", repr(questions)
    )

    return questions, {"plan_questions": questions}


@tool("think", parse_docstring=True)
def plan_think(thought: str):
    """
    Private scratchpad for reasoning only. Does NOT fetch new information or modify anything; it records concise plans/notes you will follow.

    Step 0 — Mandatory first call: On your first action you MUST call `think` once to draft the minimal inspection plan (tools to call, batched paths/queries, specific extraction goals, and stop criteria). If you attempted any other tool first, self-correct by calling `think` now.

    Step 0 — Call limits: You may call `think` up to 3 times in Step 0:
        (1) initial outline (required),
        (2) image analysis notes (only if images present),
        (3) shell/risks notes (if relevant). Include “External Context” extraction goals whenever external sources are referenced.

    Step 1 — Iterative reasoning: After each inspection tool result, you may call `think` as many times as needed to extract concrete details, resolve references (files/functions/config keys), and update the plan until it is self-contained. Do not ask the user questions here; questions, if any remain, are raised only via the final clarification tool.

    Content rules: Each note ≤ 200 words; use bullets/checklists; no external URLs; no shell commands; no user-facing prose.

    Args:
        thought (str): Your private notes/plan (≤ 200 words). Use bullets/checklists; focus on inspection steps, extraction goals, and stop criteria.

    Returns:
        A message indicating that the thought has been logged.
    """  # noqa: E501
    logger.info("[think] Thinking about: %s", thought)
    return "Thought registered."
