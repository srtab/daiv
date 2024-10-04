import textwrap

from pydantic import BaseModel, Field


class RepositoryTreeInput(BaseModel):
    """
    Obtain the tree of the repository from a given path.
    """

    path: str = Field(
        description=(
            "The path inside the repository. "
            "Empty string is the root of the repository. "
            "Use it to navigate through directories."
        )
    )
    intent: str = Field(description=("Why you're navigating to this path."))


class RepositoryFileInput(BaseModel):
    """
    Get the content of a file from the repository.
    """

    file_path: str = Field(description="The file path to get.")
    intent: str = Field(description=("Why you're obtainging this file."))


class SearchRepositoryInput(BaseModel):
    """
    Search for code snippets in the codebase.
    """

    query: str = Field(
        description=textwrap.dedent(
            """\
            The query should be a code snippet or a function/class/method name and/or include more **code-related keywords**. Focus on keywords that developers would typically use when searching for code snippets.

            ## Tips
            1. Avoid ambiguous terms in the query to get precise results.
            2. Don't use: "code", "snippet", "example", "sample", etc. as they are redundant.
            3. The query must be optimized for hybrid search: vectorstore retrieval and/or sparse retrieval.
            """  # noqa: E501
        ),
        examples=["function foo", "class CharField", "def get", "method get_foo on class User"],
    )
    intent: str = Field(description=("The intent of the search query, why you are searching for this code."))


class CommitableBaseModel(BaseModel):
    commit_message: str = Field(description="The commit message to use.")


class ReplaceSnippetWithInput(CommitableBaseModel):
    """
    Replaces a snippet in a file with the provided replacement.
    """

    file_path: str = Field(description="The file_path of code to refactor. Ignore referenced unified diff file path.")
    original_snippet: str = Field(
        description=textwrap.dedent(
            """\
            The more complete and specific, the better, to help disambiguate possible identical code in the same file.
            """
        )
    )
    replacement_snippet: str = Field(description="The replacement for the snippet.")
    commit_message: str = Field(description="The commit message to use.")


class CreateFileInput(CommitableBaseModel):
    """
    Create a new file in the repository.
    """

    file_path: str = Field(description="The file path to create.")
    content: str = Field(description="The content to insert.")


class RenameFileInput(CommitableBaseModel):
    """
    Rename a file in the repository.
    """

    file_path: str = Field(description="The file path to rename.")
    new_file_path: str = Field(description="The new file path.")


class DeleteFileInput(CommitableBaseModel):
    """
    Delete a file in the repository.
    """

    file_path: str = Field(description="The file path to delete.")


class AppendToFileInput(CommitableBaseModel):
    """
    Append content to a file in the repository.
    """

    file_path: str = Field(description="The file path to append to.")
    content: str = Field(description="The content to APPEND, including necessary newlines and indentation.")
