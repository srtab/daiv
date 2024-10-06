from typing import Annotated, TypedDict

from langchain_core.documents import Document


def reduce_documents(existing: list[Document], new: list[Document]) -> list[Document]:
    """
    This function is used to reduce the list of documents that are stored in the state.
    """
    # this is the step where the documents as retrived from the index are added to the state
    if len(existing) == 0 and len(new) > 1:
        return new
    # the document was identified as relevant, nothing need to be done
    if len(new) == 0:
        return existing
    # the document was identified as irrelevant, it should be removed from the list
    if len(existing) and len(new):
        return [item for item in existing if item not in new]
    return existing


class OverallState(TypedDict):
    query: str
    query_intent: str
    documents: Annotated[list[Document], reduce_documents]
    iterations: int


class GradeDocumentState(TypedDict):
    query: str
    query_intent: str
    document: Document
