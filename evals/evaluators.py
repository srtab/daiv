from openevals.llm import create_llm_as_judge
from openevals.prompts import CORRECTNESS_PROMPT

from automation.agent.base import BaseAgent, ThinkingLevel
from automation.agent.constants import ModelName

correctness_evaluator = create_llm_as_judge(
    prompt=CORRECTNESS_PROMPT,
    feedback_key="correctness",
    judge=BaseAgent.get_model(model=ModelName.GPT_5_1_CODEX, thinking_level=ThinkingLevel.HIGH),
)
