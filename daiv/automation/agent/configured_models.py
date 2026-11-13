"""A run's configured skill or subagent models (``AgentSettings.skill_models`` / ``subagent_models``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from automation.agent.base import get_model
from core.model_choice import EMPTY_CHOICE, ModelChoice
from core.models import THINKING_OFF
from core.models import ThinkingLevelChoices as ThinkingLevel

if TYPE_CHECKING:
    from collections.abc import Mapping

    from langchain.chat_models import BaseChatModel

    from automation.agent.agent_settings import ModelChain

logger = logging.getLogger("daiv.agent")

_reported: set[tuple[str, str]] = set()


def _log_unusable(target: str, spec: str, name: str) -> None:
    """Log the active exception at ERROR the first time this process sees ``target``'s ``spec`` fail to build, then
    at DEBUG: subagent models are built on every run, dispatched or not."""
    level = logging.DEBUG if (target, spec) in _reported else logging.ERROR
    _reported.add((target, spec))
    logger.log(level, "Configured %s model '%s' for %s cannot be used", target, spec, name, exc_info=True)


@dataclass(eq=False)
class ConfiguredModels:
    """The ``target``'s ``entries`` by name, each spec built once per run and thinking level.

    An entry's level, then the declared one, wins over ``chain``'s; ``"off"`` turns thinking off and ``""`` keeps the
    run's. A spec that cannot be built is logged at ERROR and never fails the run: the next model in line serves. So
    does a skill's spec once one of its calls failed (``call_failed``).
    """

    target: Literal["skill", "subagent"]
    entries: Mapping[str, ModelChoice] = field(default_factory=dict)
    chain: ModelChain | None = None
    _builds: dict[tuple[str, str], BaseChatModel | None] = field(default_factory=dict, init=False)

    @property
    def run_spec(self) -> str:
        return self.chain.names[0] if self.chain else ""

    def resolve_level(self, thinking_level: str) -> ThinkingLevel | None:
        """The level to build with: ``""`` is the run's, ``"off"`` is none."""
        if not thinking_level:
            return self.chain.thinking_level if self.chain else None
        return None if thinking_level == THINKING_OFF else ThinkingLevel(thinking_level)

    def build(self, spec: str, thinking_level: str = "", name: str = "the active skill") -> BaseChatModel | None:
        """``spec``'s model at ``thinking_level``, or ``None`` when it cannot be used; ``name`` is whose it is."""
        key = (spec, thinking_level)
        if key not in self._builds:
            try:
                self._builds[key] = get_model(model=spec, thinking_level=self.resolve_level(thinking_level))
            except Exception:
                # Safe to continue: the caller serves on the next model in line.
                _log_unusable(self.target, spec, name)
                self._builds[key] = None
        return self._builds[key]

    def call_failed(self, spec: str, thinking_level: str, error: Exception) -> None:
        """Stop using ``spec`` at ``thinking_level`` for the rest of the run after ``error`` failed a call."""
        logger.error("Configured %s model '%s' failed a call", self.target, spec, exc_info=error)
        self._builds[(spec, thinking_level)] = None

    def skill_choice(self, name: str, declared: ModelChoice) -> ModelChoice:
        """Skill ``name``'s model and level: each the entry's, else ``declared``'s; a model that cannot be built is
        dropped (``""`` keeps the run's)."""
        level = self.entries.get(name, EMPTY_CHOICE).thinking_level or declared.thinking_level
        for spec in (self.entries.get(name, EMPTY_CHOICE).model, declared.model):
            if spec and self.build(spec, level, name) is not None:
                return ModelChoice(model=spec, thinking_level=level)
        return ModelChoice(thinking_level=level)

    def for_subagent(
        self,
        name: str,
        declared: ModelChoice,
        run_model: BaseChatModel,
        *,
        inherited: ModelChoice = EMPTY_CHOICE,
        never_skip: bool = False,
    ) -> BaseChatModel:
        """Subagent ``name``'s model: its entry, else ``declared``, else ``inherited``, else the run's.

        The level is the entry's, else ``declared``'s, else ``inherited``'s, else the run's.
        With no entry, building ``declared``'s model raises as is, so the caller skips the subagent, unless
        ``never_skip``. A model that cannot be built never leaves the subagent out: the next in line serves.
        """
        entry = self.entries.get(name, EMPTY_CHOICE)
        level = entry.thinking_level or declared.thinking_level or inherited.thinking_level
        if entry.model and (built := self.build(entry.model, level, name)) is not None:
            return built
        if declared.model:
            try:
                return get_model(model=declared.model, thinking_level=self.resolve_level(level))
            except Exception:
                if not entry.model and not never_skip:
                    raise
                # Safe to continue: the next model in line serves. A warning: no operator chose this model.
                logger.warning("Subagent %s: its own model '%s' cannot be used", name, declared.model, exc_info=True)
        if inherited.model and (built := self.build(inherited.model, level, name)) is not None:
            return built
        if not level or not self.run_spec:
            return run_model
        return self.build(self.run_spec, level, name) or run_model
