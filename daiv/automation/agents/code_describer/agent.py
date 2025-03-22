from __future__ import annotations

import logging
from typing import TypedDict

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from automation.agents import BaseAgent

from .conf import settings
from .prompts import human, system

logger = logging.getLogger("daiv.agents")


class CodeSnippet(TypedDict):
    """
    A code snippet.
    """

    code: str
    filename: str
    language: str


class CodeDescriberAgent(BaseAgent[Runnable[CodeSnippet, str]]):
    """
    Agent to describe code snippets.
    """

    def compile(self) -> Runnable:
        """
        Compile the agent into a Runnable.

        Returns:
            Runnable: The compiled agent
        """
        prompt = ChatPromptTemplate.from_messages([("system", system), ("human", human)])
        return (
            prompt.partial(language="Not specified") | self.get_model(model=settings.MODEL_NAME) | StrOutputParser()
        ).with_config({"run_name": settings.NAME, "tags": [settings.NAME], "max_concurrency": 10})
