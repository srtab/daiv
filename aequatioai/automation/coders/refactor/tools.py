import logging

from automation.agents.models import Usage
from automation.agents.tools import FunctionTool
from automation.coders.paths_replacer.coder import PathsReplacerCoder
from automation.coders.refactor.schemas import CreateFile, ReplaceSnippetWith
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

    def codebase_retriever(self, query: str):
        docs = self.codebase_index.query(query=query, repo_id=self.repo_id, ref=self.ref, k=5)
        return [doc["path"] for doc in docs]

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
            FunctionTool(schema_model=ReplaceSnippetWith, fn=self.replace_snippet_with),
            FunctionTool(schema_model=CreateFile, fn=self.create_file),
        ]
