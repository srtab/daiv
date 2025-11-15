from __future__ import annotations

import logging
from typing import Annotated

from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.tools import tool
from openevals.llm import create_async_llm_as_judge

from automation.agents import BaseAgent
from automation.agents.plan_and_execute.prompts import review_code_changes_prompt
from codebase.context import RuntimeCtx  # noqa: TC001
from codebase.utils import GitManager, redact_diff_content  # noqa: TC001

from .conf import settings
from .schemas import ClarifyOutput, CompleteOutput, PlanOutput

logger = logging.getLogger("daiv.tools")


PLAN_THINK_TOOL_NAME = "think"
PLAN_THINK_TOOL_DESCRIPTION = f"""\
Use this tool to outline your investigation approach and track progress through complex tasks. This is a planning and progress-tracking tool ONLY - it does NOT fetch information or modify anything.

**When to use:**
- Planning which files/patterns to search for before investigating
- Tracking progress on multi-step investigations
- Updating your task list as you discover new requirements

**When NOT to use:**
- Summarizing your final plan (use `{PlanOutput.__name__}` instead)
- Concluding your investigation (call an output tool immediately)
- Saying 'ready to create plan' or 'all clear' (call `{PlanOutput.__name__}` NOW)

**CRITICAL:** If your `plan` field contains phrases like:
- 'Ready to plan'
- 'Ready to create implementation plan'
- 'All information is clear'
- 'Now I'll create the plan'

Then you should call `{PlanOutput.__name__}`, `{ClarifyOutput.__name__}`, or `{CompleteOutput.__name__}` instead of this tool.

**Usage rules:**
- Does NOT fetch new information - use investigation tools for that
- Mark tasks as completed immediately when done, don't batch them
- Update or remove tasks as you learn new information
- Skip using this tool if the task is simple/straightforward

Being proactive with task management demonstrates attentiveness and ensures you complete all requirements successfully.
"""  # noqa: E501


REVIEW_CODE_CHANGES_TOOL_NAME = "review_code_changes"
REVIEW_CODE_CHANGES_TOOL_DESCRIPTION = """\
Verifies that code changes are correct and complete by evaluating them against the original plan.

**Usage rules:**
- Use this tool when you have finished making all the changes for the plan and want to verify their correctness.
- This is a **vital step** before marking the task as complete - always review your changes before finishing.
- The tool will automatically evaluate the changes you made against the plan tasks.
- If the review fails, you will receive specific reasoning about what needs to be fixed.
"""  # noqa: E501


@tool(PLAN_THINK_TOOL_NAME, description=PLAN_THINK_TOOL_DESCRIPTION)
def plan_think_tool(
    thought: Annotated[
        str,
        "Your investigation approach or progress update in markdown format. "
        "Should contain tasks to complete, not final conclusions.",
    ],
) -> str:
    """
    Tool to help llm outline investigation approach and track progress through complex tasks.
    """  # noqa: E501
    logger.info("[%s] Thinking notes: %s", plan_think_tool.name, thought)
    return "Your thought has been logged."


@tool(REVIEW_CODE_CHANGES_TOOL_NAME, description=REVIEW_CODE_CHANGES_TOOL_DESCRIPTION)
async def review_code_changes_tool(
    placeholder: Annotated[str, "Unused parameter (for compatibility). Leave empty."], runtime: ToolRuntime[RuntimeCtx]
) -> str:
    """
    Tool to let llm review code changes against the original plan.
    """  # noqa: E501
    logger.info("[%s] Reviewing code changes...", review_code_changes_tool.name)

    git_manager = GitManager(runtime.context.repo)

    if not git_manager.is_dirty():
        return "No changes have been made yet to review."

    evaluator = create_async_llm_as_judge(
        prompt=review_code_changes_prompt,
        judge=BaseAgent.get_model(
            model=settings.CODE_REVIEW_MODEL_NAME, thinking_level=settings.CODE_REVIEW_THINKING_LEVEL
        ),
    )
    inputs = [task.model_dump(mode="json") for task in runtime.state["plan_tasks"]]
    outputs = redact_diff_content(git_manager.get_diff(), runtime.context.config.omit_content_patterns)

    result = await evaluator(inputs=inputs, outputs=outputs)

    if result["score"] is False:
        logger.info("[%s] Review code changes fail: %s", review_code_changes_tool.name, result["comment"])
        return f"FAIL: {result['comment']}"
    return "PASS"
