from textwrap import dedent

from pydantic import BaseModel, Field


class ReplyReviewer(BaseModel):
    """
    Provide a reply to the reviewer's comment or question.
    """

    reply: str = Field(
        description=dedent(
            """\
            - The reply MUST be under 100 words.
            - Format your response using appropriate markdown for code snippets, lists, or emphasis where needed.
            """
        )
    )
