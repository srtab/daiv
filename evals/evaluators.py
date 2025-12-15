from openevals.llm import create_llm_as_judge
from openevals.prompts import CORRECTNESS_PROMPT

from automation.agents.base import BaseAgent, ThinkingLevel
from automation.agents.constants import ModelName

correctness_evaluator = create_llm_as_judge(
    prompt=CORRECTNESS_PROMPT,
    feedback_key="correctness",
    judge=BaseAgent.get_model(model=ModelName.GPT_5_1_CODEX_MINI, thinking_level=ThinkingLevel.HIGH),
)
