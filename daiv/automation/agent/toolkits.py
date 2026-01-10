from __future__ import annotations

from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.tools.base import BaseTool


class BaseToolkit(metaclass=ABCMeta):
    @classmethod
    @abstractmethod
    def get_tools(cls) -> list[BaseTool]:
        """
        Get the tools for the toolkit.
        """
