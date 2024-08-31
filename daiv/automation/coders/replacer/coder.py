from typing import Unpack, cast

from pydantic import BaseModel, Field

from automation.agents.agent import LlmAgent
from automation.agents.models import Message
from automation.coders.base import Coder
from automation.coders.typings import ReplacerInvoke

from .prompts import ReplacerPrompts


class ReplacedCodeSnippet(BaseModel):
    content: str = Field(description="The content of the resulting snippet after replacement.")


class ReplacerCoder(Coder[ReplacerInvoke, str | None]):
    """
    A coder that replaces a reference snippet with a replacement snippet in a given content.
    """

    def invoke(self, *args, **kwargs: Unpack[ReplacerInvoke]) -> str | None:
        """
        Ask an agent to replace a reference snippet with a replacement snippet in a given content.
        """
        agent = LlmAgent(
            memory=[
                Message(role="system", content=ReplacerPrompts.format_system_msg()),
                Message(
                    role="user",
                    content=ReplacerPrompts.format_default_msg(
                        original_snippet=kwargs["original_snippet"],
                        replacement_snippet=kwargs["replacement_snippet"],
                        content=kwargs["content"],
                    ),
                ),
            ]
        )
        response = agent.run(response_model=ReplacedCodeSnippet)

        self.usage += agent.usage

        return cast(ReplacedCodeSnippet, response).content
