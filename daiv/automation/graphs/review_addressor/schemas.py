from pydantic import BaseModel, Field

from automation.graphs.schemas import AskForClarification
from automation.graphs.schemas import Plan as BasePlan

DETERMINE_NEXT_ACTION_TOOL_NAME = "determine_next_action"


class HumanFeedbackResponse(BaseModel):
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
            "Set to True if you want to show the diff hunk to the executor agent; otherwise, False. "
            "Only show the diff hunk if the reviewer asks to revert changes, "
            "to help the executor recover the required code."
        )
    )


# need rewrite the class `DetermineNextActionResponse` to use the redefined class `Plan`.
class DetermineNextActionResponse(BaseModel):
    action: Plan | AskForClarification = Field(description="The next action to be taken.")
