from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseAgent, ThinkingLevel

__all__ = ["BaseAgent", "ThinkingLevel"]


# Deferred so importing light submodules (e.g. automation.agent.display) does not
# pull the langchain/langgraph stack (~160MB) into every Django process at setup.
def __getattr__(name: str):
    if name in __all__:
        from automation.agent import base

        return getattr(base, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
