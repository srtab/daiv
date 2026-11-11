"""A run's configured skill or subagent models (``AgentSettings.skill_models`` / ``subagent_models``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from automation.agent.base import get_model

if TYPE_CHECKING:
    from collections.abc import Mapping

    from langchain.chat_models import BaseChatModel

    from automation.agent.agent_settings import ModelChain

logger = logging.getLogger("daiv.agent")


@dataclass(eq=False)
class ConfiguredModels:
    """The ``target``'s ``entries`` by name, each spec built once per run with ``chain``'s thinking level.

    A spec that cannot be built is logged at ERROR and never fails the run: the next model in line serves.
    """

    target: Literal["skill", "subagent"]
    entries: Mapping[str, str] = field(default_factory=dict)
    chain: ModelChain | None = None
    _builds: dict[str, BaseChatModel | None] = field(default_factory=dict, init=False)

    def build(self, spec: str, name: str = "the active skill") -> BaseChatModel | None:
        """``spec``'s model, or ``None`` when it cannot be used; ``name`` is whose it is, for the log."""
        if spec not in self._builds:
            try:
                self._builds[spec] = get_model(
                    model=spec, thinking_level=self.chain.thinking_level if self.chain else None
                )
            except Exception:
                # Safe to continue: the caller serves on the next model in line.
                logger.exception("Configured %s model '%s' for %s cannot be used", self.target, spec, name)
                self._builds[spec] = None
        return self._builds[spec]

    def skill_spec(self, name: str, file_spec: str | None) -> str | None:
        """The spec skill ``name`` runs on: its entry, else ``file_spec``; ``None`` keeps the run's model."""
        for spec in (self.entries.get(name), file_spec):
            if spec and self.build(spec, name) is not None:
                return spec
        return None

    def for_subagent(
        self, name: str, file_spec: str, run_model: BaseChatModel, *, never_skip: bool = False
    ) -> BaseChatModel:
        """Subagent ``name``'s model: its entry, else ``file_spec``, else ``run_model``.

        With no entry, building ``file_spec`` raises as is, so the caller skips the subagent, unless ``never_skip``.
        An entry that cannot be built never leaves the subagent out: ``file_spec``, else ``run_model``, serves.
        """
        configured = self.entries.get(name)
        if configured and (built := self.build(configured, name)) is not None:
            return built
        if not file_spec:
            return run_model
        if not configured and not never_skip:
            return get_model(model=file_spec)
        try:
            return get_model(model=file_spec)
        except Exception:
            # Safe to continue: the run's model serves.
            logger.exception("Subagent %s: its own model '%s' cannot be used", name, file_spec)
            return run_model
