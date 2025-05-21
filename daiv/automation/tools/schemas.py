import textwrap
from typing import Annotated, Any

from langgraph.prebuilt.tool_node import InjectedStore
from pydantic import Base64Bytes, BaseModel, Field


class SearchCodeSnippetsInput(BaseModel):
    """
    Search for code snippets in the codebase.
    """

    query: str = Field(
        ...,
        description=textwrap.dedent(
            """\
            A code-centric search term including code snippets, function/class/method names, code-related keywords or file paths.

            Tips:
            1. Avoid ambiguous terms for precise results.
            2. Do not use redundant words like "code", "snippet", "example", or "sample".
            3. Optimize the query for hybrid search methods (vector and sparse retrieval).
            """  # noqa: E501
        ),
    )
    intent: str = Field(..., description="A brief description of why you are searching for this code.")


class CrossSearchCodeSnippetsInput(SearchCodeSnippetsInput):
    """
    Search for code snippets in specific repositories or all repositories.
    """

    repository: str | None = Field(
        default=None,
        description=(
            "The name of the repository to search in. "
            "If not provided, the search will be performed in all repositories."
        ),
    )


class RepositoryStructureInput(BaseModel):
    """
    Get the structure of the repository.
    """

    intent: str = Field(description="A brief description of why you are getting the repository structure.")


class RetrieveFileContentInput(BaseModel):
    """
    Get the content of a file from the repository.
    """

    file_paths: list[str] = Field(
        description=(
            "The paths to the files to retrieve (e.g., 'example/tests/test_admin.py'). "
            "You can provide multiple file paths to retrieve the content of multiple files at once. "
        )
    )
    intent: str = Field(description="A description of why you're getting these files.")
    store: Annotated[Any, InjectedStore()]


class CommitableBaseModel(BaseModel):
    commit_message: str = Field(
        ...,
        description=(
            "The commit message to use. This will be used to describe the changes applied. "
            "Tip: Use action-oriented verbs, such as 'Added', 'Updated', 'Removed', 'Improved', etc..."
        ),
    )


class ReplaceSnippetInFileInput(CommitableBaseModel):
    """
    Replaces a snippet in a file with the provided replacement.
    """

    file_path: str = Field(..., description="The path to the file where the replacement will take place.")
    original_snippet: str = Field(
        ...,
        description=(
            "The exact sequence of line to be replaced, including **all indentation and spacing**.\n"
            "Tip: Copy the snippet directly from the file to ensure an exact match."
        ),
    )
    replacement_snippet: str = Field(
        ...,
        description=(
            "The new sequence of lines to replace the original, including the necessary indentation and "
            "spacing to fit seamlessly into the code.\n"
            "Tip: Align the indentation level with the surrounding code for consistency."
        ),
    )
    store: Annotated[Any, InjectedStore()]


class CreateNewRepositoryFileInput(CommitableBaseModel):
    """
    Create a new file in the repository.
    """

    file_path: str = Field(..., description="The path within the repository where the new file will be created.")
    file_content: str = Field(..., description="The content of the new repository file.")
    store: Annotated[Any, InjectedStore()]


class RenameRepositoryFileInput(CommitableBaseModel):
    """
    Rename a file in the repository.
    """

    file_path: str = Field(..., description="The path of the file to be renamed within the repository.")
    new_file_path: str = Field(..., description="The new path and name for the file.")
    store: Annotated[Any, InjectedStore()]


class DeleteRepositoryFileInput(CommitableBaseModel):
    """
    Delete a file in the repository.
    """

    file_path: str = Field(..., description="The path of the file to delete within the repository.")
    store: Annotated[Any, InjectedStore()]


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
