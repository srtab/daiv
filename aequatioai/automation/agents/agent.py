import json
import logging
from abc import ABC
from typing import TYPE_CHECKING

import litellm
from decouple import config
from litellm import completion
from openai._types import NotGiven

from .models import Message, ToolCall, Usage
from .tools import FunctionTool

if TYPE_CHECKING:
    from litellm.utils import ModelResponse

logger = logging.getLogger(__name__)

litellm.telemetry = False


class LlmAgent(ABC):
    name: str
    memory: list[Message]
    tools: list[FunctionTool]
    model: str = "gpt-4-turbo-2024-04-09"

    iterations: int = 0
    max_iterations: int = 10

    def __init__(
        self,
        name: str = "Agent",
        memory: list[Message] | None = None,
        tools: list[FunctionTool] | None = None,
        model: str | None = None,
        stop_message: str | None = None,
    ):
        self.name = name
        self.memory = memory or []
        self.tools = tools or []
        self.model = model or self.model
        self.stop_message = stop_message
        self.usage = Usage()
        self.api_key = config("OPENAI_API_KEY")

    def single_run(self) -> str | None:
        """
        Run the agent until it reaches a stopping condition.
        """
        logger.debug("----[%s] Running Agent----", self.name)
        logger.debug("Previous messages: ")

        if logger.isEnabledFor(logging.DEBUG):
            for message in self.memory:
                logger.debug("%s: %s", message.role, message.content)

        self.run_iteration()

        if self.iterations == self.max_iterations:
            raise Exception("Agent %s exceeded the maximum number of iterations without finishing.", self.name)

        return self.memory[-1].content

    def run(self, single_iteration: bool = False) -> str | None:
        """
        Run the agent until it reaches a stopping condition.
        """
        logger.debug("----[%s] Running Agent----", self.name)
        logger.debug("Previous messages: ")

        if logger.isEnabledFor(logging.DEBUG):
            for message in self.memory:
                logger.debug("%s: %s", message.role, message.content)

        while self.should_continue_iteration(single_iteration=single_iteration):
            self.run_iteration()

        if self.iterations == self.max_iterations:
            raise Exception("Agent %s exceeded the maximum number of iterations without finishing.", self.name)

        return self.memory[-1].content

    def run_iteration(self):
        """
        Run a single iteration of the agent.
        """
        logger.debug("----[%s] Running iteration {%d}----", self.name, self.iterations)

        messages = [{k: v for k, v in msg.model_dump().items() if v is not None} for msg in self.memory]

        response: ModelResponse = completion(
            model=self.model,
            messages=messages,
            temperature=0,
            api_key=self.api_key,
            tools=([tool.to_dict() for tool in self.tools] if self.tools else NotGiven()),
        )

        message = Message(**response.choices[0].message.model_dump())

        self.memory.append(message)

        logger.debug("Message content:\n%s", message.content)
        logger.debug("Message tool calls:\n%s", message.tool_calls)

        if message.tool_calls:
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
            logger.debug("Total tokens:\n%s", self.usage.total_tokens)

    def should_continue_iteration(self, single_iteration: bool = False) -> bool:
        """
        Check if the agent should continue running iterations.
        """
        if self.iterations == 0:
            return True

        if single_iteration:
            return False

        if self.stop_message:
            if self.memory[-1].role not in ["assistant", "model"]:
                return False

            if self.memory[-1].content and self.stop_message in self.memory[-1].content:
                return False

        if self.memory[-1].content is None:
            return False

        return self.iterations < self.max_iterations

    @property
    def max_input_tokens(self) -> int:
        """
        The maximum number of tokens that can be passed to the model.
        """
        return litellm.get_max_tokens(self.model)

    def token_count(self, messages: list[Message]) -> int | None:
        """
        Count the tokens in a list of messages.
        """
        return litellm.token_counter(model=self.model, messages=messages)

    def call_tool(self, tool_call: ToolCall):
        logger.debug("[%s] Calling tool %s with arguments %r", tool_call.id, tool_call.function, tool_call.kwargs)

        tool = next(tool for tool in self.tools if tool.name == tool_call.function)

        tool_result = tool.call(**tool_call.kwargs)

        logger.debug("Tool %s returned \n%s", tool_call.function, tool_result)

        return Message(role="tool", content=tool_result, tool_call_id=tool_call.id)
