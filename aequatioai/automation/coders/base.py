import abc
from typing import Generic, TypeVar, Unpack

from automation.agents.models import Usage

from .typings import Invoke

TInvoke = TypeVar("TInvoke", bound=Invoke)
TInvokeReturn = TypeVar("TInvokeReturn")


class Coder(abc.ABC, Generic[TInvoke, TInvokeReturn]):
    usage: Usage

    def __init__(self, usage: Usage):
        self.usage = usage

    @abc.abstractmethod
    def invoke(self, *args, **kwargs: Unpack[TInvoke]) -> TInvokeReturn:
        pass
