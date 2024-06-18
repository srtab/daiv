from typing import Unpack, cast

from automation.agents.agent import LlmAgent
from automation.agents.models import Message, Usage
from automation.coders.base import Coder
from automation.coders.change_describer.models import ChangesDescription
from automation.coders.change_describer.prompts import ChangeDescriberPrompts
from automation.coders.typings import ChangeDescriberInvoke


class ChangeDescriberCoder(Coder[ChangeDescriberInvoke, ChangesDescription | None]):
    """
    A coder that generates a description of the changes.
    """

    def invoke(self, *args, **kwargs: Unpack[ChangeDescriberInvoke]) -> ChangesDescription | None:
        """
        Invoke the coder to generate a description of the changes.
        """
        agent = LlmAgent(
            memory=[
                Message(role="system", content=ChangeDescriberPrompts.format_system_msg()),
                Message(role="user", content=ChangeDescriberPrompts.format_default_msg(changes=kwargs["changes"])),
            ],
        )

        changes_description = agent.run(single_iteration=True, response_model=ChangesDescription)

        self.usage += agent.usage

        return cast(ChangesDescription, changes_description) if changes_description is not None else None


if __name__ == "__main__":
    changes_description = ChangeDescriberCoder(Usage()).invoke(
        changes=["Add a new feature", "Fix a bug", "Refactor code"]
    )
    print(changes_description)