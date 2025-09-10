from __future__ import annotations

import json
import logging

from langchain_core.tools import tool

from .schemas import PLAN_DESCRIPTION, ChangeInstructions, Plan

logger = logging.getLogger("daiv.tools")


PLAN_TOOL_NAME = "plan"
CLARIFY_TOOL_NAME = "clarify"
COMPLETE_TOOL_NAME = "complete"
PLAN_THINK_TOOL_NAME = "think"

FINALIZE_TOOLS = [PLAN_TOOL_NAME, CLARIFY_TOOL_NAME, COMPLETE_TOOL_NAME]


@tool(
    PLAN_TOOL_NAME,
    args_schema=Plan,
    description=PLAN_DESCRIPTION,
    return_direct=True,
    response_format="content_and_artifact",
)
def plan_tool(changes: list[ChangeInstructions]) -> tuple[str, dict]:
    """
    Deliver a self-contained implementation plan that satisfies the user's request.

    **Usage rules:**
    - The requirements are clear and changes are needed.
    - The context is sufficient to deliver the plan with confidence.

    Args:
        changes (list[ChangeInstructions]): The plan to execute.

    Returns:
        tuple[str, dict]: The plan to execute.
    """  # noqa: E501
    logger.info("[%s] The plan to execute: %s", PLAN_TOOL_NAME, repr(changes))

    return json.dumps([change.model_dump() for change in changes]), {"plan_tasks": changes}


@tool(CLARIFY_TOOL_NAME, return_direct=True, response_format="content_and_artifact", parse_docstring=True)
def clarify_tool(questions: str) -> tuple[str, dict]:
    """
    Deliver targeted grounded questions to clarify the inspection.

    **Usage rules:**
    - There's uncertainty about the requirements/changes needed.
    - The context is insufficient to deliver the plan with confidence.
    - The user needs to provide additional details that could not be covered by the context.

    Args:
        questions (str): Targeted concise, direct and to the point questions. No chit-chat. Ground them in the codebase and search results; use markdown formatting for `variables`, `files`, `directories`, `dependencies` as needed.

    Returns:
        tuple[str, dict]: The targeted grounded questions to ask the user for clarification.
    """  # noqa: E501
    logger.info("[%s] Clarifying the inspection: %s", CLARIFY_TOOL_NAME, questions)

    return questions, {"plan_questions": questions}


@tool(COMPLETE_TOOL_NAME, return_direct=True, response_format="content_and_artifact", parse_docstring=True)
def complete_tool(message: str) -> tuple[str, dict]:
    """
    Deliver a message to confirm no changes or actions are needed.

    **Usage rules:**
    - The context is sufficient to confirm no changes or actions are needed.
    - The current state meets the requirements.

    Args:
        message (str): The message to demonstrate how current state meets requirements with specific evidence.

    Returns:
        tuple[str, dict]: The message to complete the inspection.
    """
    logger.info("[%s] No changes needed: %s", COMPLETE_TOOL_NAME, message)
    return message, {"no_changes_needed": message}


@tool(PLAN_THINK_TOOL_NAME, parse_docstring=True)
def plan_think_tool(plan: str):
    """
    Use this tool to outline what you need to investigate to assist the user. This helps you track progress and organize complex tasks in a structured way.

    **Usage rules:**
    - Does NOT fetch new information or modify anything, it's just a placeholder to help you track progress.
    - Update tasks as you learn new information to help you track progress.
    - **Important:** It is critical that you mark tasks as completed as soon as you are done with them. Do not batch up multiple tasks before marking them as completed.

    **Skip using this tool when:**
    - There is only a single, straightforward task
    - The task is trivial and tracking it provides no organizational benefit
    - The task can be completed in less than 3 trivial steps
    - The task is purely conversational or informational

    Args:
        plan (str): The plan to investigate.

    Returns:
        A message indicating that the thought has been registered.
    """  # noqa: E501
    logger.info("[%s] Thinking about: %s", plan_think_tool.name, plan)
    return "Thought registered."
