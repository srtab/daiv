import abc
import logging
from typing import Generic

from automation.coders.base import Coder, TInvoke, TInvokeReturn
from codebase.clients import RepoClient
from codebase.indexers import CodebaseIndex
from codebase.models import RepositoryFile

from .prompts import RefactorPrompts

logger = logging.getLogger(__name__)


class RefactorCoder(Coder[TInvoke, TInvokeReturn], abc.ABC, Generic[TInvoke, TInvokeReturn]):
    repo_client: RepoClient
    codebase_index: CodebaseIndex

    def __init__(self, repo_client: RepoClient):
        self.repo_client = repo_client
        self.codebase_index = CodebaseIndex(repo_client)

    def get_repo_files_prompt(self, repo_file_list: list[RepositoryFile]) -> str:
        """
        Get the content of the files in the repository as a prompt.
        """
        for repo_file in repo_file_list:
            repo_file.content = self.repo_client.get_repository_file(
                repo_file.repo_id, repo_file.file_path, ref=repo_file.ref
            )
        return RefactorPrompts.repository_files_to_str(repo_file_list)
