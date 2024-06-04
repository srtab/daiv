import logging
import textwrap

from automation.agents.models import Usage
from automation.agents.tools import FunctionTool
from automation.coders.paths_replacer.coder import PathsReplacerCoder
from automation.coders.replacer import ReplacerCoder
from codebase.base import FileChange
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

logger = logging.getLogger(__name__)


class CodeActionTools:
    def __init__(
        self, repo_client: RepoClient, codebase_index: CodebaseIndex, usage: Usage, repo_id: str, ref: str | None = None
    ):
        self.repo_client = repo_client
        self.codebase_index = codebase_index
        self.usage = usage
        self.repo_id = repo_id
        self.ref = ref
        self.file_changes: dict[str, FileChange] = {}

    def replace_snippet_with(
        self, file_path: str, original_snippet: str, replacement_snippet: str, commit_message: str
    ):
        """
        Replaces a snippet with the provided replacement.
        """
        logger.debug(
            "[CodeActionTools.replace_snippet_with] Replacing snippet\n```\n%s\n```\n with \n```\n%s\n```\nin %s",
            original_snippet,
            replacement_snippet,
            file_path,
        )

        if file_path not in self.file_changes:
            repo_file_content = self.repo_client.get_repository_file(self.repo_id, file_path, self.ref)
        else:
            repo_file_content = self.file_changes[file_path].content

        # TODO: optimize to avoid calling this too many times, the repository tree should be cached in some way.
        repository_tree = self.codebase_index.extract_tree(self.repo_id, self.ref)

        replacement_snippet_result = PathsReplacerCoder(self.usage).invoke(
            code_snippet=replacement_snippet, repository_tree=repository_tree
        )

        if replacement_snippet_result is None:
            logger.warning("No paths extracted from the replacement snippet.")

        replaced_content = ReplacerCoder(self.usage).invoke(
            original_snippet=original_snippet, replacement_snippet=replacement_snippet_result, content=repo_file_content
        )

        if not replaced_content:
            raise Exception("Snippet replacement failed.")

        # Add a trailing snippet to the new snippet to match the original snippet if there isn't already one.
        if not replaced_content.endswith("\n"):
            replaced_content += "\n"

        if file_path in self.file_changes:
            self.file_changes[file_path].content = replaced_content
            self.file_changes[file_path].commit_messages.append(commit_message)
        else:
            self.file_changes[file_path] = FileChange(
                action="update", file_path=file_path, content=replaced_content, commit_messages=[commit_message]
            )

        return f"success: Resulting code after replacement:\n```\n{replaced_content}\n```\n"

    def create_file(self, file_path: str, content: str, commit_message: str):
        """
        Creates a new file with the provided content.
        """
        logger.debug("[CodeActionTools.create_file] Creating new file %s", file_path)

        if file_path in self.file_changes:
            raise Exception("File already exists.")

        # TODO: optimize to avoid calling this too many times, the repository tree should be cached in some way.
        repository_tree = self.codebase_index.extract_tree(self.repo_id, self.ref)

        replacement_content = PathsReplacerCoder(self.usage).invoke(
            code_snippet=content, repository_tree=repository_tree
        )

        self.file_changes[file_path] = FileChange(
            action="create", file_path=file_path, content=replacement_content, commit_messages=[commit_message]
        )

        return f"success: Created new file {file_path}"

    def get_tools(self):
        return [
            FunctionTool(
                name="replace_snippet_with",
                description=textwrap.dedent(
                    """\
                    Use this as the primary tool to write code changes to a file.

                    Replaces a snippet in a file with the provided replacement.
                    - The snippet must be an exact match.
                    - The replacement can be any string.
                    - The original snippet must be an entire line, not just a substring of a line. It should also include the indentation and spacing.
                    - Indentation and spacing must be included in the replacement snippet.
                    - If multiple replacements needed, call this function multiple times.
                    """  # noqa: E501
                ),
                parameters=[
                    {
                        "name": "file_path",
                        "type": "string",
                        "description": "The file_path of code to refactor. Ignore referenced unified diff file path.",
                    },
                    {"name": "original_snippet", "type": "string", "description": "The snippet to replace."},
                    {
                        "name": "replacement_snippet",
                        "type": "string",
                        "description": "The replacement for the snippet.",
                    },
                    {"name": "commit_message", "type": "string", "description": "The commit message to use."},
                ],
                fn=self.replace_snippet_with,
                required=["file_path", "original_snippet", "replacement_snippet", "commit_message"],
            ),
            FunctionTool(
                name="create_file",
                description="""Use this as primary tool to create a new file with the provided content.""",
                parameters=[
                    {"name": "file_path", "type": "string", "description": "The file path to create."},
                    {"name": "content", "type": "string", "description": "The content to insert."},
                    {"name": "commit_message", "type": "string", "description": "The commit message to use."},
                ],
                fn=self.create_file,
                required=["file_path", "content", "commit_message"],
            ),
        ]
