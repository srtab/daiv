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
from .schemas import ClarifyOutput, CompleteOutput, FinishOutput, PlanOutput

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
- When the task is simple/straightforward (e.g., the change is obvious from the codebase)

**CRITICAL:** If your `plan` field contains phrases like:
- 'Ready to plan'
- 'Ready to create implementation plan'
- 'All information is clear'
- 'Now I'll create the plan'
- 'The change is straightforward'

Then you should call `{PlanOutput.__name__}`, `{ClarifyOutput.__name__}`, or `{CompleteOutput.__name__}` instead of this tool.

**Usage rules:**
- Does NOT fetch new information - use investigation tools for that
- Mark tasks as completed immediately when done, don't batch them
- Update or remove tasks as you learn new information

Being proactive with task management demonstrates attentiveness and ensures you complete all requirements successfully.
"""  # noqa: E501


REVIEW_CODE_CHANGES_TOOL_NAME = "review_code_changes"
REVIEW_CODE_CHANGES_TOOL_DESCRIPTION = f"""\
**INTERNAL VERIFICATION TOOL** — Validates that applied changes match the plan requirements.

**Purpose:**
- Automated quality gate for your edits before calling `{FinishOutput.__name__}`.
- Returns PASS (changes are correct) or FAIL (with reasons for what needs fixing).

**Usage rules:**
- Call this tool ONLY after completing all edits in a cycle (Step 2 of the workflow).
- **Rate limit: ≤3 total calls per session.** Use it strategically at natural checkpoints.
- Act on FAIL results by fixing identified issues, then optionally re-review (consumes another call).

**CRITICAL — Do NOT mention this tool in {FinishOutput.__name__}:**

**When NOT to use:**
- During discovery (Step 0/1) — no changes exist yet to review.
- After already calling `{FinishOutput.__name__}` — the session is over.
- To verify individual file edits — this tool evaluates **all changes** against the **entire plan**.
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
