from pydantic import BaseModel, Field

from automation.agents.schemas import AskForClarification
from automation.agents.schemas import Plan as BasePlan


class RespondReviewerResponse(BaseModel):
    """
    Provide a final response to the reviewer.
    """

    response: str = Field(
        description=(
            "Answer in the first person, without asking if they want more changes. E.g., "
            "'The changes you requested have been made.'"
        )
    )


class Plan(BasePlan):
    show_diff_hunk_to_executor: bool = Field(
        description=(
            "Set to True if is relevant to show the diff hunk to the executor agent; otherwise, False. "
            "A relevant situation to show the diff hunk is if the reviewer asks to revert changes, "
            "to help the executor recover the required code."
        )
    )


# need rewrite the class `DetermineNextActionResponse` to use the redefined class `Plan`.
class DetermineNextActionResponse(BaseModel):
    action: Plan | AskForClarification = Field(description="The next action to be taken.")
