from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.retrievers import ContextualCompressionRetriever
from langchain_core.documents import Document
from langchain_core.runnables import Runnable

from automation.agents import BaseAgent

from .compressors import CodebaseSearchReranker
from .conf import settings

if TYPE_CHECKING:
    from langchain_core.retrievers import BaseRetriever


logger = logging.getLogger("daiv.agents")


class CodebaseSearchAgent(BaseAgent[Runnable[str, list[Document]]]):
    """
    Agent to search for code snippets in the codebase.
    """

    def __init__(self, retriever: BaseRetriever, intent: str | None = None, *args, **kwargs):
        self.retriever = retriever
        self.intent = intent
        super().__init__(*args, **kwargs)

    async def compile(self) -> Runnable:
        """
        Compile the agent into a Runnable.

        Returns:
            Runnable: The compiled agent
        """
        compressor = CodebaseSearchReranker.from_llm(
            llm=BaseAgent.get_model(model=settings.RERANKING_MODEL_NAME), top_n=settings.TOP_N, intent=self.intent
        )

        return ContextualCompressionRetriever(base_compressor=compressor, base_retriever=self.retriever).with_config({
            "run_name": settings.NAME
        })
