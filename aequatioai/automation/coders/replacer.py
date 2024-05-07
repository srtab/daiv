from typing import Unpack

from aequatioai.automation.agents.agent import LlmAgent
from aequatioai.automation.agents.models import Message
from aequatioai.automation.agents.prompts import ReplacerPrompts
from aequatioai.automation.coders.base import Coder
from aequatioai.automation.coders.typings import ReplacerInvoke
from aequatioai.automation.utils import extract_text_inside_tags


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
                Message(
                    role="user",
                    content=ReplacerPrompts.format_default_msg(
                        original_snippet=kwargs["original_snippet"],
                        replacement_snippet=kwargs["replacement_snippet"],
                        content=kwargs["content"],
                        commit_message=kwargs["commit_message"],
                    ),
                ),
            ]
        )
        if (response := agent.run(single_iteration=True)) is None:
            return None

        return extract_text_inside_tags(response, "code", strip_newlines=True)
