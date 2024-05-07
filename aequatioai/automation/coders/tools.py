import logging
import textwrap

from aequatioai.automation.agents.tools import FunctionTool
from aequatioai.automation.coders.replacer import ReplacerCoder
from aequatioai.automation.utils import find_original_snippet
from aequatioai.codebase.clients import RepoClient

logging.basicConfig(level=logging.DEBUG)

logger = logging.getLogger(__name__)


class CodeActionTools:
    snippet_matching_threshold = 0.8
    chunk_padding = 16

    def __init__(self, repo_client: RepoClient, repo_id: str, ref: str | None = None):
        self.repo_client = repo_client
        self.repo_id = repo_id
        self.ref = ref

    def replace_snippet_with(
        self,
        file_path: str,
        original_snippet: str,
        replacement_snippet: str,
        commit_message: str,
    ):
        """
        Replaces a snippet with the provided replacement.
        """
        logger.debug(
            "[CodeActionTools.replace_snippet_with] Replacing snippet\n```\n%s\n```\n with \n```\n%s\n```\nin %s",
            original_snippet, replacement_snippet, file_path
        )

        repo_file_content = self.repo_client.get_repository_file(self.repo_id, file_path, self.ref).decode()

        replaced_content = ReplacerCoder().invoke(
            replacement_snippet=replacement_snippet,
            original_snippet=original_snippet,
            content=repo_file_content,
            commit_message=commit_message,
        )

        if not replaced_content:
            raise Exception("Snippet replacement failed.")

        # Add a trailing snippet to the new snippet to match the original snippet if there isn't already one.
        if not replaced_content.endswith("\n"):
            replaced_content += "\n"

        print(f"####### success: Resulting code after replacement:\n```\n{replaced_content}\n```")

        return f"success: Resulting code after replacement:\n```\n{replaced_content}\n```\n"

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
                    - If multiple replacements needed, call this function multiple times."""
                ),
                parameters=[
                    {
                        "name": "file_path",
                        "type": "string",
                        "description": "The file path to modify.",
                    },
                    {
                        "name": "original_snippet",
                        "type": "string",
                        "description": "The snippet to replace.",
                    },
                    {
                        "name": "replacement_snippet",
                        "type": "string",
                        "description": "The replacement for the snippet.",
                    },
                    {
                        "name": "commit_message",
                        "type": "string",
                        "description": "The commit message to use.",
                    },
                ],
                fn=self.replace_snippet_with,
            ),
        ]
