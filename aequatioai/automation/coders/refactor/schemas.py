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
    """

    file_path: str = Field(description="The file_path of code to refactor. Ignore referenced unified diff file path.")
    original_snippet: str = Field(description="The snippet to replace.")
    replacement_snippet: str = Field(description="The replacement for the snippet.")
    commit_message: str = Field(description="The commit message to use.")


class CreateFile(OpenAISchema):
    """
    Use this as primary tool to create a new file with the provided content.
    """

    file_path: str = Field(description="The file path to create.")
    content: str = Field(description="The content to insert.")
    commit_message: str = Field(description="The commit message to use.")
