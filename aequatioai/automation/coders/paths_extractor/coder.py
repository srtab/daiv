from typing import Unpack

from automation.agents.agent import LlmAgent
from automation.agents.models import Message
from automation.coders.base import Coder
from automation.coders.paths_extractor.prompts import PathsExtractorPrompts
from automation.coders.typings import PathsExtractorInvoke


class PathsReplacerCoder(Coder[PathsExtractorInvoke, str | None]):
    """
    A coder that extracts project paths from a code snippet.
    """

    def invoke(self, *args, **kwargs: Unpack[PathsExtractorInvoke]) -> str | None:
        """
        Ask an agent to search for project paths and then use that information replace with new ones.
        """
        agent = LlmAgent(
            memory=[
                Message(role="system", content=PathsExtractorPrompts.format_system_msg()),
                Message(
                    role="user", content=PathsExtractorPrompts.format_default_msg(code_snippet=kwargs["code_snippet"])
                ),
            ],
            response_format="json",
        )

        if (response := agent.run(single_iteration=True)) is None:
            return None

        replaced_code_snippet = kwargs["code_snippet"]

        for path in response:
            replaced_code_snippet = replaced_code_snippet.replace(
                path["file_path"], path["file_path"].replace("feedportal", "bkcf_onboarding")
            )

        return replaced_code_snippet
