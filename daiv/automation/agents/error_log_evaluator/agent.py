from typing import TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from automation.agents import BaseAgent
from automation.conf import settings

from .prompts import human, system


class ErrorLogEvaluatorInput(TypedDict):
    log_trace_1: str
    log_trace_2: str


class ErrorLogEvaluatorOutput(BaseModel):
    is_same_error: bool = Field(description="Whether the two logs are the same error")
    justification: str = Field(description="The justification for the decision")


class ErrorLogEvaluatorAgent(BaseAgent[Runnable[ErrorLogEvaluatorInput, ErrorLogEvaluatorOutput]]):
    """
    Agent to evaluate if two error logs are the same error or related.
    """

    model_name = settings.GENERIC_COST_EFFICIENT_MODEL_NAME

    def compile(self) -> Runnable:
        prompt = ChatPromptTemplate.from_messages([system, human])
        return prompt | self.model.with_structured_output(ErrorLogEvaluatorOutput, method="json_schema")
