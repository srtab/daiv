from abc import ABC, abstractmethod

from pydantic import BaseModel
from pydantic_core import ErrorDetails  # noqa: TC002


class UnprocessableEntityResponse(BaseModel):
    """
    Response for Unprocessable Entity
    """

    detail: list[ErrorDetails]


class BaseCallback(BaseModel, ABC):
    """
    Base class for all callbacks.
    """

    @abstractmethod
    def accept_callback(self) -> bool:
        pass

    @abstractmethod
    def process_callback(self):
        pass
