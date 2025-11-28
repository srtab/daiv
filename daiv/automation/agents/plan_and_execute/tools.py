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
from .schemas import FinishOutput

logger = logging.getLogger("daiv.tools")


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
