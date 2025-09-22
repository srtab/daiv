from abc import ABC, abstractmethod

from pydantic import BaseModel
from pydantic_core import ErrorDetails

from codebase.context import RepositoryCtx


class UnprocessableEntityResponse(BaseModel):
    """
    Response for Unprocessable Entity
    """

    detail: list[ErrorDetails]


class BaseCallback(BaseModel, ABC):
    """
    Base class for all callbacks.
    """

    _ctx: RepositoryCtx

    @abstractmethod
    def accept_callback(self) -> bool:
        pass

    @abstractmethod
    def process_callback(self):
        pass

    def set_ctx(self, ctx: RepositoryCtx):
        self._ctx = ctx
