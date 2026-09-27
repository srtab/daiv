from functools import cache

from openevals.llm import create_async_llm_as_judge
from openevals.prompts import CORRECTNESS_PROMPT
from pydantic import BaseModel

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


class Verdict(BaseModel):
    passed: bool
    explanation: str


@cache
def _question_judge():
    return BaseAgent.get_model(model=ModelName.CLAUDE_OPUS_4_6, thinking_level=ThinkingLevel.MEDIUM)


async def judge_question_relevance(request: str, rendered_questions: str) -> Verdict:
    """Whether the agent's questions target the ambiguity that most changes the work."""
    prompt = (
        "A coding agent working in a repository received the request below and, instead of doing the work, "
        "asked the user the questions that follow.\n\n"
        f"Request:\n{request}\n\nQuestions:\n{rendered_questions}\n\n"
        "Pass the questions only if they target the ambiguity that most changes the work, and none of them asks "
        "about a routine judgment call or something the agent could learn by reading the repository."
    )
    result = await _question_judge().with_structured_output(Verdict).ainvoke(prompt)
    return result or Verdict(passed=False, explanation="the judge returned nothing")
