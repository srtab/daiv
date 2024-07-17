import abc
from typing import Generic, TypeVar, Unpack

from automation.agents.models import Usage
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

from .typings import Invoke

STOP_MESSAGE = "<DONE>"

TInvoke = TypeVar("TInvoke", bound=Invoke)
TInvokeReturn = TypeVar("TInvokeReturn")


class Coder(abc.ABC, Generic[TInvoke, TInvokeReturn]):
    usage: Usage

    def __init__(self, usage: Usage):
        self.usage = usage

    @abc.abstractmethod
    def invoke(self, *args, **kwargs: Unpack[TInvoke]) -> TInvokeReturn:
        pass


class CodebaseCoder(Coder[TInvoke, TInvokeReturn], abc.ABC, Generic[TInvoke, TInvokeReturn]):
    repo_client: RepoClient
    codebase_index: CodebaseIndex

    def __init__(self, usage: Usage):
        super().__init__(usage)
        self.repo_client = RepoClient.create_instance()
        self.codebase_index = CodebaseIndex(self.repo_client)
