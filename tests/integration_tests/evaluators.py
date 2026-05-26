from functools import cache

from openevals.llm import create_async_llm_as_judge
from openevals.prompts import CORRECTNESS_PROMPT

from automation.agent.base import BaseAgent, ThinkingLevel
from automation.agent.constants import ModelName


@cache
def get_correctness_evaluator():
    """Build the LLM-as-judge evaluator on first use.

    Deferred until call time so module import does not require a configured
    Provider table — the fixture in conftest.py provisions provider rows
    after the test DB is set up, which is after module import.
    """
    return create_async_llm_as_judge(
        prompt=CORRECTNESS_PROMPT,
        feedback_key="correctness",
        judge=BaseAgent.get_model(model=ModelName.GPT_5_3_CODEX, thinking_level=ThinkingLevel.MEDIUM),
    )
