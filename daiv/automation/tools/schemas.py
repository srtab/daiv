import textwrap
from typing import Annotated, Any

from langgraph.prebuilt.tool_node import InjectedStore
from pydantic import BaseModel, Field


class SearchCodeSnippetsInput(BaseModel):
    """
    Search for code snippets in the codebase.
    """

    query: str = Field(
        ...,
        description=textwrap.dedent(
            """\
            A code-centric search term including code snippets, function/class/method names, or code-related keywords.

            Tips:
            1. Avoid ambiguous terms for precise results.
            2. Do not use redundant words like "code", "snippet", "example", or "sample".
            3. Optimize the query for hybrid search methods (vector and sparse retrieval).
            """  # noqa: E501
        ),
        examples=["function foo", "class CharField", "def get", "method get_foo on class User"],
    )
    intent: str = Field(..., description=("A brief description of why you are searching for this code."))


class ExploreRepositoryPathInput(BaseModel):
    """
    Obtain the tree of the repository from a given path.
    """

    path: str = Field(
        ...,
        description=(
            "The path inside the repository to navigate. An empty string '' represents the root of the repository."
        ),
    )
    intent: str = Field(..., description=("A description of why you're navigating to this path."))


class RetrieveFileContentInput(BaseModel):
    """
    Get the content of a file from the repository.
    """

    file_path: str = Field(..., description="The path to the file to retrieve (e.g., 'example/tests/test_admin.py').")
    intent: str = Field(..., description=("A description of why you're obtaining this file"))
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
