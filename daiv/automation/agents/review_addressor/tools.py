from typing import Literal

from langchain_core.tools import tool
from langgraph.graph import END
from langgraph.types import Command


@tool("reply_reviewer", parse_docstring=True)
def reply_reviewer_tool(reply: str) -> Command[Literal["__end__"]]:
    """
    Use this tool to reply to the reviewer's comments or questions.

    Args:
        reply (str): The reply to the reviewer's comments or questions. The reply MUST be under 100 words.
                     Reply using the same language as the reviewer's comment.

    Returns:
        Command[Literal["__end__"]]: The next step in the workflow.
    """
    return Command(goto=END, update={"reply": reply}, graph=Command.PARENT)
