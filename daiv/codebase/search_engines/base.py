from abc import ABC, abstractmethod

from langchain_core.documents import Document
from pydantic import BaseModel


class ScoredResult(BaseModel):
    score: float
    document: Document


class SearchEngine(ABC):
    """
    Base class for search engines.
    """

    @abstractmethod
    def add_documents(self, index_name: str, documents: list[Document]):
        pass

    @abstractmethod
    def delete_documents(self, index_name: str, field_name: str, field_value: str | list[str]):
        pass

    @abstractmethod
    def delete(self, index_name: str):
        pass

    @abstractmethod
    def search(self, index_name: str, query: str, k: int = 10, **kwargs) -> list[ScoredResult]:
        pass
