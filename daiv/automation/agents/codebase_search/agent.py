from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.retrievers.document_compressors import LLMListwiseRerank
from langchain_core.documents import Document
from langchain_core.runnables import Runnable, RunnableLambda, RunnablePassthrough

from automation.agents import BaseAgent
from automation.conf import settings
from automation.retrievers import MultiQueryRephraseRetriever

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.retrievers import BaseRetriever

logger = logging.getLogger("daiv.agents")


class CodebaseSearchAgent(BaseAgent[Runnable[str, list[Document]]]):
    """
    Agent to search for code snippets in the codebase.
    """

    model_name = settings.CODING_COST_EFFICIENT_MODEL_NAME

    def __init__(self, retriever: BaseRetriever, *args, **kwargs):
        self.retriever = retriever
        super().__init__(*args, **kwargs)

    def compile(self) -> Runnable:
        """
        Compile the agent into a Runnable.
        """
        return {
            "query": RunnablePassthrough(),
            "documents": MultiQueryRephraseRetriever.from_llm(self.retriever, llm=self.model),
        } | RunnableLambda(
            lambda inputs: self._compress_documents(inputs["documents"], inputs["query"]), name="compress_documents"
        )

    def get_model_kwargs(self) -> dict:
        kwargs = super().get_model_kwargs()
        kwargs["temperature"] = 0.5
        return kwargs

    def _compress_documents(self, documents: list[Document], query: str) -> Sequence[Document]:
        """
        Compress the documents using a multi-query retriever and a listwise reranker.

        Args:
            documents (Sequence[Document]): The documents to compress
            query (str): The search query string

        Returns:
            Sequence[Document]: The compressed documents
        """
        reranker = LLMListwiseRerank.from_llm(llm=self.model, top_n=5)
        return reranker.compress_documents(documents, query)
