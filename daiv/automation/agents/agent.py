from __future__ import annotations

import json
import logging
from abc import ABC
from functools import cached_property
from typing import TYPE_CHECKING

import litellm
from decouple import config
from litellm import completion
from openai._types import NotGiven

from .models import Message, ToolCall, Usage

if TYPE_CHECKING:
    from litellm.utils import ModelResponse
    from pydantic import BaseModel

    from .tools import FunctionTool

logger = logging.getLogger(__name__)

litellm.telemetry = False


CHEAPER_MODEL = "gpt-4o-mini"
PERFORMANT_MODEL = "gpt-4o-2024-08-06"


class LlmAgent(ABC):
    """
    An agent that interacts with the LLM API.
    """

    name: str
    memory: list[Message]
    tools: list[FunctionTool]
    model: str = PERFORMANT_MODEL

    iterations: int = 0
    max_iterations: int = 20

    def __init__(
        self,
        name: str = "Agent",
        memory: list[Message] | None = None,
        tools: list[FunctionTool] | None = None,
        model: str | None = None,
        stop_messages: list[str] | None = None,
    ):
        self.name = name
        self.memory = memory or []
        self.tools = tools or []
        self.model = model or self.model
        self.stop_messages = stop_messages or []
        self.usage = Usage()
        self.api_key = config("OPENAI_API_KEY")

    def run(
        self, single_iteration: bool = False, response_model: type[BaseModel] | None = None
    ) -> str | BaseModel | None:
        """
        Run the agent until it reaches a stopping condition.
        """
        logger.debug("----[%s] Running Agent----", self.name)
        logger.debug("Previous messages: ")

        if logger.isEnabledFor(logging.DEBUG):
            for message in self.memory:
                logger.debug("%s: %s", message.role, message.content)

        while self.should_continue_iteration(single_iteration=single_iteration):
            self.run_iteration(response_model)

        if self.iterations == self.max_iterations:
            raise Exception("Agent %s exceeded the maximum number of iterations without finishing.", self.name)

        self.iterations = 0

        if response_model is not None:
            return self.memory[-1].model_instance

        return self.memory[-1].content

    def run_iteration(self, response_model: type[BaseModel] | None = None):
        """
        Run a single iteration of the agent.
        """
        logger.debug("----[%s] Running iteration {%d}----", self.name, self.iterations)

        messages = [{k: v for k, v in msg.model_dump().items() if v is not None} for msg in self.memory]

        completion_kwargs = {"model": self.model, "messages": messages, "temperature": 0, "api_key": self.api_key}

        response: ModelResponse = completion(
            tools=([tool.to_schema() for tool in self.tools] if self.tools else NotGiven()),
            response_format=response_model,
            **completion_kwargs,
        )

        message = Message(**response.choices[0].message.json())

        self.memory.append(message)

        logger.debug("%s: %s", message.role, message.content)

        # If the response has a model instance, avoid call the tools, it means the model has already done the job.
        if response_model and message.content:
            message.model_instance = response_model.model_validate_json(message.content)
        elif message.tool_calls:
            for tool_call in message.tool_calls:
                tool_response = self.call_tool(
                    ToolCall(
                        id=tool_call.id,
                        function=tool_call.function.name,
                        kwargs=json.loads(tool_call.function.arguments),
                    )
                )

                self.memory.append(tool_response)

        self.iterations += 1

        if response.usage:
            self.usage.completion_tokens = response.usage.completion_tokens
            self.usage.prompt_tokens = response.usage.prompt_tokens
            self.usage.total_tokens = response.usage.total_tokens
            self.usage.cost = litellm.completion_cost(response)
            logger.debug("Total tokens:\n%s", self.usage.total_tokens)

    def should_continue_iteration(self, single_iteration: bool = False) -> bool:
        """
        Check if the agent should continue running iterations.
        """
        if self.iterations == 0:
            return True

        if single_iteration or self.iterations >= self.max_iterations or self.memory[-1].model_instance is not None:
            return False

        if (
            self.stop_messages
            and self.memory[-1].content
            and any(stop_message for stop_message in self.stop_messages if stop_message in self.memory[-1].content)
        ):
            return False

        if self.memory[-1].role not in ["assistant", "model"]:
            return True

        if self.memory[-1].content is None:
            return False

        return self.iterations < self.max_iterations

    @cached_property
    def max_input_tokens(self) -> int | None:
        """
        The maximum number of tokens that can be passed to the model.
        """
        return litellm.get_max_tokens(self.model)

    def token_count(self, messages: list[Message]) -> int | None:
        """
        Count the tokens in a list of messages.
        """
        return litellm.token_counter(model=self.model, messages=messages)

    def call_tool(self, tool_call: ToolCall) -> Message:
        """
        Call a tool with the provided arguments.
        """
        logger.debug("[%s] Calling tool %s with arguments %r", tool_call.id, tool_call.function, tool_call.kwargs)

        tool = next(tool for tool in self.tools if tool.name == tool_call.function)
        tool_result = tool.call(**tool_call.kwargs)

        if not isinstance(tool_result, str):
            tool_result = str(tool_result)

        logger.debug("Tool %s returned \n%s", tool_call.function, tool_result)

        return Message(role="tool", content=tool_result, tool_call_id=tool_call.id)
