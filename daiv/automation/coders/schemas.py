from instructor import OpenAISchema
from pydantic import Field


class ReplaceSnippetWith(OpenAISchema):
    """
    Use this as the primary tool to write code changes to a file.

    Replaces a snippet in a file with the provided replacement.
    - The snippet must be an exact match.
    - The replacement can be any string.
    - The original snippet must be an entire line, not just a substring of a line. It should also include the indentation and spacing.
    - Indentation and spacing must be included in the replacement snippet.
    - If multiple replacements needed, call this function multiple times.
    """  # noqa: E501

    file_path: str = Field(description="The file_path of code to refactor. Ignore referenced unified diff file path.")
    original_snippet: str = Field(description="The snippet to replace.")
    replacement_snippet: str = Field(description="The replacement for the snippet.")
    commit_message: str = Field(description="The commit message to use.")


class CreateFile(OpenAISchema):
    """
    Use this as primary tool to create a new file with the provided content.

    If the file already exists, it will raise an error. Only use this to create inexistent files.
    """

    file_path: str = Field(description="The file path to create.")
    content: str = Field(description="The content to insert.")
    commit_message: str = Field(description="The commit message to use.")


class RenameFile(OpenAISchema):
    """
    Use this as the primary tool to rename a file.
    """

    file_path: str = Field(description="The file path to rename.")
    new_file_path: str = Field(description="The new file path.")
    commit_message: str = Field(description="The commit message to use.")


class DeleteFile(OpenAISchema):
    """
    Use this as the primary tool to delete a file.
    """

    file_path: str = Field(description="The file path to delete.")
    commit_message: str = Field(description="The commit message to use.")


class GetRepositoryFile(OpenAISchema):
    """
    Use this as the primary tool to get the content of a file from a repository.
    """

    file_path: str = Field(description="The file path to get.")


class GetRepositoryTree(OpenAISchema):
    """
    Use this as the primary tool to help find files or folders in the repository.
    """

    path: str = Field(
        description=(
            "The path inside the repository. "
            "The default is the root of the repository. "
            "Use it to navigate through directories."
        ),
        default="",
    )


class CodebaseSearch(OpenAISchema):
    """
    Search for code snippets in the codebase.
    Providing long and detailed queries with entire code snippets will yield better results.
    """

    query: str = Field(description=("The query to search for."))
