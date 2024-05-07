import abc
from typing import Generic, TypeVar, Unpack

from .typings import Invoke

TInvoke = TypeVar("TInvoke", bound=Invoke)
TInvokeReturn = TypeVar("TInvokeReturn")


class Coder(abc.ABC, Generic[TInvoke, TInvokeReturn]):

    @abc.abstractmethod
    def invoke(self, *args, **kwargs: Unpack[TInvoke]) -> TInvokeReturn:
        pass
