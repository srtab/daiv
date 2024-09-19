from typing import TypedDict

from langchain_core.documents import Document


class OverallState(TypedDict):
    query: str
    query_intent: str
    documents: list[Document]
    iterations: int
