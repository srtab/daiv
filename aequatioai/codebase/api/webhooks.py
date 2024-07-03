from abc import ABC, abstractmethod

from pydantic import BaseModel
from pydantic_core import ErrorDetails


class UnprocessableEntityResponse(BaseModel):
    """
    Response for Unprocessable Entity
    """

    detail: list[ErrorDetails]


class BaseWebHook(BaseModel, ABC):
    """
    Base class for all webhooks
    """

    @abstractmethod
    def accept_webhook(self) -> bool:
        pass

    @abstractmethod
    def process_webhook(self):
        pass
