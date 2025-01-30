from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from langchain.retrievers import ContextualCompressionRetriever
from langchain.retrievers.document_compressors import LLMListwiseRerank
from langchain_core.documents import Document
from langchain_core.runnables import Runnable

from automation.agents import BaseAgent
from automation.conf import settings
from automation.retrievers import MultiQueryRephraseRetriever

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.retrievers import BaseRetriever


logger = logging.getLogger("daiv.agents")


class CodebaseSearchAgent(BaseAgent[Runnable[str, list[Document]]]):
    """
    Agent to search for code snippets in the codebase.
    """

    model_name = settings.CODING_COST_EFFICIENT_MODEL_NAME
    fallback_model_name = settings.GENERIC_COST_EFFICIENT_MODEL_NAME

    def __init__(self, retriever: BaseRetriever, rephrase: bool = True, *args, **kwargs):
        self.retriever = retriever
        self.rephrase = rephrase
        super().__init__(*args, **kwargs)

    def compile(self) -> Runnable:
        """
        Compile the agent into a Runnable.

        Returns:
            Runnable: The compiled agent
        """
        if self.rephrase:
            base_retriever = MultiQueryRephraseRetriever.from_llm(
                self.retriever, llm=self.model.with_fallbacks([cast("BaseChatModel", self.fallback_model)])
            )
        else:
            base_retriever = self.retriever

        return ContextualCompressionRetriever(
            base_compressor=LLMListwiseRerank.from_llm(
                llm=self.model.with_fallbacks([cast("BaseChatModel", self.fallback_model)]),
                top_n=settings.CODEBASE_SEARCH_TOP_N,
            ),
            base_retriever=base_retriever,
        )
