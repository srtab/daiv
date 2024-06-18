import abc
import logging
from typing import Generic

from automation.agents.models import Usage
from automation.coders.base import Coder, TInvoke, TInvokeReturn
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

logger = logging.getLogger(__name__)


class RefactorCoder(Coder[TInvoke, TInvokeReturn], abc.ABC, Generic[TInvoke, TInvokeReturn]):
    repo_client: RepoClient
    codebase_index: CodebaseIndex

    def __init__(self, usage: Usage):
        super().__init__(usage)
        self.repo_client = RepoClient.create_instance()
        self.codebase_index = CodebaseIndex(self.repo_client)
