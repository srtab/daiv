from typing import Unpack, cast

from automation.agents.agent import CHEAPER_MODEL, LlmAgent
from automation.agents.models import Message
from automation.coders.base import Coder
from automation.coders.change_describer.models import ChangesDescriber
from automation.coders.change_describer.prompts import ChangesDescriberPrompts
from automation.coders.typings import ChangesDescriberInvoke


class ChangesDescriberCoder(Coder[ChangesDescriberInvoke, ChangesDescriber | None]):
    """
    A coder that generates a description of the changes.
    """

    def invoke(self, *args, **kwargs: Unpack[ChangesDescriberInvoke]) -> ChangesDescriber | None:
        """
        Invoke the coder to generate a description of the changes.
        """
        agent = LlmAgent(
            model=CHEAPER_MODEL,  # Use a cheaper model for this task
            memory=[
                Message(role="system", content=ChangesDescriberPrompts.format_system_msg()),
                Message(role="user", content=ChangesDescriberPrompts.format_default_msg(changes=kwargs["changes"])),
            ],
        )

        changes_description = agent.run(single_iteration=True, response_model=ChangesDescriber)

        self.usage += agent.usage

        return cast(ChangesDescriber, changes_description) if changes_description is not None else None
