import textwrap

from pydantic import BaseModel, Field


class SearchCodeSnippetsInput(BaseModel):
    """
    Search for code snippets in the codebase.
    """

    query: str = Field(
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
    intent: str = Field(description=("A brief description of why you are searching for this code."))


class ExploreRepositoryPathInput(BaseModel):
    """
    Obtain the tree of the repository from a given path.
    """

    path: str = Field(
        description=(
            "The path inside the repository to navigate. An empty string '' represents the root of the repository."
        )
    )
    intent: str = Field(description=("A description of why you're navigating to this path."))


class RetrieveFileContentInput(BaseModel):
    """
    Get the content of a file from the repository.
    """

    file_path: str = Field(description="The path to the file to retrieve (e.g., 'webhooks/tests/test_admin.py').")
    intent: str = Field(description=("A description of why you're obtaining this file"))


class CommitableBaseModel(BaseModel):
    commit_message: str = Field(
        description=(
            "The commit message to use. This will be used to describe the changes. "
            "Use an imperative tone, such as 'Add', 'Update', 'Remove'."
        )
    )


class ReplaceSnippetInFileInput(CommitableBaseModel):
    """
    Replaces a snippet in a file with the provided replacement.
    """

    file_path: str = Field(description="The path to the file where the replacement will take place.")
    original_snippet: str = Field(
        description=(
            "The exact snippet to be replaced, including indentation and spacing.\n"
            "Tip: Copy the snippet directly from the file to ensure an exact match."
        )
    )
    replacement_snippet: str = Field(
        description=(
            "The new snippet to replace the original, including necessary indentation and spacing.\n"
            "Tip: Align the indentation level with the surrounding code for consistency."
        )
    )


class CreateNewRepositoryFileInput(CommitableBaseModel):
    """
    Create a new file in the repository.
    """

    file_path: str = Field(description="The path within the repository where the new file will be created.")
    content: str = Field(description="The content to insert into the new file.")


class RenameRepositoryFileInput(CommitableBaseModel):
    """
    Rename a file in the repository.
    """

    file_path: str = Field(description="The current path of the file to rename.")
    new_file_path: str = Field(description="The new path and name for the file.")


class DeleteRepositoryFileInput(CommitableBaseModel):
    """
    Delete a file in the repository.
    """

    file_path: str = Field(description="The path of the file to delete within the repository.")


class AppendToRepositoryFileInput(CommitableBaseModel):
    """
    Append content to a file in the repository.
    """

    file_path: str = Field(description="The path of the file to append to within the repository.")
    content: str = Field(description="The content to append, including necessary newlines and indentation.")
