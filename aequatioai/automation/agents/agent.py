import logging
from abc import ABC
import os

import litellm
from litellm.utils import ModelResponse

from .models import Message, Usage
from .tools import FunctionTool

logger = logging.getLogger(__name__)

litellm.telemetry = False

class LlmAgent(ABC):
    model: str = "gpt-4-turbo-2024-04-09"
    name: str
    tools: list[FunctionTool]
    memory: list[Message]

    def __init__(
        self,
        name: str = "Agent",
        memory: list[Message] | None = None,
        tools: list[FunctionTool] | None = None,
    ):
        self.tools = tools or []
        self.memory = memory or []
        self.usage = Usage()
        self.name = name
    
    def run(self) -> str | None:
        """
        Run the agent on a prompt.
        """
        logger.debug(f"----[{self.name}] Running Agent----")
        logger.debug("Previous messages: ")
        for message in self.memory:
            logger.debug(f"{message.role}: {message.content}")

        messages = [{k: v for k, v in msg.model_dump().items() if v is not None} for msg in self.memory]

        response: ModelResponse = litellm.completion(model=self.model, messages=messages, temperature=0)
        
        message = Message(**response.choices[0].message.model_dump())

        self.memory.append(message)

        logger.debug(f"Message content:\n{message.content}")
        logger.debug(f"Message tool calls:\n{message.tool_calls}")

        if message.tool_calls:
            raise NotImplementedError("Tool calls are not yet implemented.")

        if response.usage:
            self.usage.completion_tokens = response.usage.completion_tokens
            self.usage.prompt_tokens = response.usage.prompt_tokens
            self.usage.total_tokens = response.usage.total_tokens

        print("Total tokens:", self.usage.total_tokens)
        
        return self.memory[-1].content

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
