from typing import Annotated, Any

from langgraph.prebuilt.tool_node import InjectedStore
from pydantic import Base64Bytes, BaseModel, Field


class RunCommandResult(BaseModel):
    """
    The result of running a command in the sandbox.
    """

    command: str
    output: str
    exit_code: int


class RunCommandInput(BaseModel):
    """
    Run a command in the sandbox.
    """

    commands: list[str] = Field(..., description="The commands to run in the sandbox.")
    intent: str = Field(..., description=("A description of why you're running these commands."))
    store: Annotated[Any, InjectedStore()]


class RunCommandResponse(BaseModel):
    """
    The response from running commands in the sandbox.
    """

    results: list[RunCommandResult]
    archive: Base64Bytes | None


class RunCodeInput(BaseModel):
    """
    Run python code.
    """

    dependencies: list[str] = Field(
        default_factory=list, description="The dependencies to install before running the code."
    )
    python_code: str = Field(
        ...,
        description=(
            "The python code to be evaluated. The contents will be in main.py. "
            "Tip: Use the `print` function to output results."
        ),
    )
    intent: str = Field(..., description=("A description of why you're running this code."))


class WebSearchInput(BaseModel):
    """
    Perform a web search to retrieve up-to-date information.
    """

    query: str = Field(
        ...,
        description=(
            "Search query to look up relevant information in the web. "
            "The query should be focused and specific to get accurate results."
        ),
    )
    intent: str = Field(
        ...,
        description=(
            "A brief description of why you are performing this web search and how it relates to the user's request."
        ),
    )
