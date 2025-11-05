from __future__ import annotations

import logging

from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.tools import tool
from openevals.llm import create_async_llm_as_judge

from automation.agents import BaseAgent
from automation.agents.plan_and_execute.prompts import review_code_changes_prompt
from automation.utils import get_file_changes
from codebase.context import RuntimeCtx  # noqa: TC001

from .conf import settings

logger = logging.getLogger("daiv.tools")


PLAN_THINK_TOOL_NAME = "think"
REVIEW_CODE_CHANGES_TOOL_NAME = "review_code_changes"


@tool(PLAN_THINK_TOOL_NAME, parse_docstring=True)
def plan_think_tool(plan: str):
    """
    Use this tool to outline what you need to investigate to assist the user. This helps you track progress and organize complex tasks in a structured way.

    **Usage rules:**
    - Does NOT fetch new information or modify anything, it's just a placeholder to help you track progress.
    - Add any new follow-up tasks as you discover them during your investigation.
    - You can also update future tasks, such as deleting them if they are no longer necessary, or adding new tasks that are necessary. Don't change previously completed tasks.
    - **Important:** It is critical that you mark tasks as completed as soon as you are done with them. Do not batch up multiple tasks before marking them as completed.

    **Skip using this tool when:**
    - There is only a single, straightforward task
    - The task is trivial and tracking it provides no organizational benefit
    - The task can be completed in less than 3 trivial steps
    - The task is purely conversational or informational

    Being proactive with task management demonstrates attentiveness and ensures you complete all requirements successfully.

    Args:
        plan (str): The plan to investigate in markdown format.

    Returns:
        A message indicating that the thought has been registered.
    """  # noqa: E501
    logger.info("[%s] Thinking about: %s", plan_think_tool.name, plan)
    return "Thought registered."


@tool(REVIEW_CODE_CHANGES_TOOL_NAME, parse_docstring=True)
async def review_code_changes_tool(placeholder: str, runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Verifies that code changes are correct and complete by evaluating them against the original plan.

    **Usage rules:**
    - Use this tool when you have finished making all the changes for the plan and want to verify their correctness.
    - This is a **vital step** before marking the task as complete - always review your changes before finishing.
    - The tool will automatically evaluate the changes you made against the plan tasks.
    - If the review fails, you will receive specific reasoning about what needs to be fixed.

    Args:
        placeholder: Unused parameter (for compatibility). Leave empty.

    Returns:
        The result of the review code changes tool evaluation.
    """  # noqa: E501
    logger.info("[%s] Reviewing code changes", review_code_changes_tool.name)

    file_changes = await get_file_changes(runtime.store)
    if not file_changes:
        return "No changes have been made yet to review."

    diffs = [file_change.diff_hunk for file_change in file_changes if file_change.diff_hunk]
    if not diffs:
        return "No changes have been made yet to review."

    evaluator = create_async_llm_as_judge(
        prompt=review_code_changes_prompt,
        judge=BaseAgent.get_model(
            model=settings.CODE_REVIEW_MODEL_NAME, thinking_level=settings.CODE_REVIEW_THINKING_LEVEL
        ),
    )
    inputs = [task.model_dump(mode="json") for task in runtime.state["plan_tasks"]]
    outputs = "\n".join(diffs)

    result = await evaluator(inputs=inputs, outputs=outputs)

    if result["score"] is False:
        return f"FAIL: {result['comment']}"
    return "PASS"
