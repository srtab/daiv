import logging
from typing import Unpack

from automation.agents.agent import LlmAgent
from automation.agents.models import Message
from automation.coders.base import Coder
from automation.coders.change_describer.models import ChangesDescription
from automation.coders.change_describer.prompts import ChangeDescriberPrompts
from automation.coders.typings import ChangeDescriberInvoke

logger = logging.getLogger(__name__)


class ChangeDescriberCoder(Coder[ChangeDescriberInvoke, ChangesDescription | None]):
    """
    A coder that generates a description of the changes.
    """

    def invoke(self, *args, **kwargs: Unpack[ChangeDescriberInvoke]) -> ChangesDescription | None:
        """
        Invoke the coder to generate a description of the changes.
        """
        agent = LlmAgent[dict](
            memory=[
                Message(role="system", content=ChangeDescriberPrompts.format_system_msg()),
                Message(role="user", content=ChangeDescriberPrompts.format_default_msg(changes=kwargs["changes"])),
            ],
            response_format="json",
        )

        changes_description = agent.run(single_iteration=True)

        self.usage += agent.usage

        if changes_description is None:
            return None

        return ChangesDescription(**changes_description)
