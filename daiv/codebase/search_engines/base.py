from abc import ABC, abstractmethod

from langchain_core.documents import Document

from codebase.models import CodebaseNamespace


class SearchEngine(ABC):
    """
    Base class for search engines.
    """

    @abstractmethod
    def add_documents(self, namespace: CodebaseNamespace, documents: list[Document]):
        pass

    @abstractmethod
    def delete_documents(self, namespace: CodebaseNamespace, source: str | list[str]):
        pass

    @abstractmethod
    def delete(self, namespace: CodebaseNamespace):
        pass
