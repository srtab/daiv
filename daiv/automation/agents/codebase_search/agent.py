from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from langchain.retrievers import ContextualCompressionRetriever
from langchain.retrievers.document_compressors import LLMListwiseRerank
from langchain_core.documents import Document
from langchain_core.runnables import Runnable

from automation.agents import BaseAgent
from automation.retrievers import MultiQueryRephraseRetriever

from .conf import settings

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.retrievers import BaseRetriever


logger = logging.getLogger("daiv.agents")


class CodebaseSearchAgent(BaseAgent[Runnable[str, list[Document]]]):
    """
    Agent to search for code snippets in the codebase.
    """

    def __init__(self, retriever: BaseRetriever, rephrase: bool = True, *args, **kwargs):
        self.retriever = retriever
        self.rephrase = rephrase
        super().__init__(*args, **kwargs)

    async def compile(self) -> Runnable:
        """
        Compile the agent into a Runnable.

        Returns:
            Runnable: The compiled agent
        """
        if self.rephrase:
            base_retriever: BaseRetriever = MultiQueryRephraseRetriever.from_llm(
                self.retriever, llm=cast("BaseChatModel", self.get_model(model=settings.REPHRASE_MODEL_NAME))
            )
        else:
            base_retriever: BaseRetriever = self.retriever

        return ContextualCompressionRetriever(
            base_compressor=LLMListwiseRerank.from_llm(
                llm=cast("BaseChatModel", self.get_model(model=settings.RERANKING_MODEL_NAME)), top_n=settings.TOP_N
            ),
            base_retriever=base_retriever,
        ).with_config({"run_name": settings.NAME})
