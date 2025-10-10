from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from pydantic_core import ErrorDetails


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
