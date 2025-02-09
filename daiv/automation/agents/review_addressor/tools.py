from typing import Literal

from langchain_core.tools import tool
from langgraph.graph import END
from langgraph.types import Command

from .schemas import ReplyReviewer


@tool("reply_reviewer", args_schema=ReplyReviewer)
def reply_reviewer_tool(reply: str) -> Command[Literal["__end__"]]:
    """
    Use this tool to reply to the reviewer's comments or questions.
    """
    return Command(goto=END, update={"reply": reply}, graph=Command.PARENT)
