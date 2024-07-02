from abc import ABC, abstractmethod

from pydantic import BaseModel


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
