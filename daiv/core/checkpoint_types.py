"""Registry of the pydantic models allowed to revive from checkpointed agent state.

Register every pydantic model kept in agent state from the owning app's ``AppConfig.ready()``; an unregistered
one revives as its raw ``lc:2`` dict. Kept apart from ``core.checkpointer``, which imports langgraph, so
``ready()`` can register types without loading the agent stack during ``django.setup()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel

_registered: set[type[BaseModel]] = set()


def register_checkpoint_type(cls: type[BaseModel]) -> None:
    """Let ``DAIVRedisSerializer`` revive ``cls`` from a checkpoint; registering twice is a no-op."""
    _registered.add(cls)


def registered_checkpoint_types() -> tuple[type[BaseModel], ...]:
    return tuple(_registered)
