from pydantic import BaseModel, Field


class ReplyReviewer(BaseModel):
    """
    Provide a reply to the reviewer's comment or question.
    """

    reply: str = Field(
        description=(
            "Reply in the first person, without asking if they want more changes. E.g., "
            "'The changes you requested have been made.'"
        )
    )
