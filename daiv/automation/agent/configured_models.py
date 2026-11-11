"""A run's configured skill or subagent models (``AgentSettings.skill_models`` / ``subagent_models``): each spec built
once per run, and an entry that cannot be built, or whose call fails, reported once, naming the model that served
instead."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from automation.agent.base import get_model
from automation.agent.middlewares.subagent_models import SubagentModelFailoverMiddleware
from automation.agent.model_spec import ModelSpecError
from automation.agent.notices import ModelFallbackNotice

if TYPE_CHECKING:
    from collections.abc import Mapping

    from langchain.agents.middleware import AgentMiddleware
    from langchain.chat_models import BaseChatModel

    from automation.agent.agent_settings import ModelChain
    from automation.agent.base import ThinkingLevel
    from automation.agent.notices import FallbackReason, NoticeTarget, RunNotices

logger = logging.getLogger("daiv.agent")


def build_configured_model(*, spec: str, thinking_level: ThinkingLevel | None) -> BaseChatModel | FallbackReason:
    """The model ``spec`` names, or why it cannot be used."""
    try:
        return get_model(model=spec, thinking_level=thinking_level)
    except ModelSpecError:
        return "unknown_provider"
    except RuntimeError:
        return "provider_unavailable"
    except Exception:
        # Safe to continue: the caller serves on the model it would use with no configured entry and reports it.
        logger.exception("Configured model '%s' failed to initialize", spec)
        return "init_failed"


@dataclass(eq=False)
class ConfiguredModels:
    """The ``target``'s ``entries`` by name, built with ``chain``'s thinking level.

    An entry that cannot be built is logged at ERROR and recorded on ``notices``, naming ``chain``'s first model when
    the run's model serves instead: a skill's when it is invoked, a subagent's when it is dispatched. A spec whose call
    fails is reported the same way (``areport_call_failure``) and not used again in the run.
    """

    target: NoticeTarget
    entries: Mapping[str, str] = field(default_factory=dict)
    chain: ModelChain | None = None
    notices: RunNotices | None = None
    _builds: dict[str, BaseChatModel | FallbackReason] = field(default_factory=dict, init=False)
    _reported: set[str] = field(default_factory=set, init=False)
    _undispatched: dict[str, tuple[str, str | None, FallbackReason]] = field(default_factory=dict, init=False)
    _failed: set[str] = field(default_factory=set, init=False)
    _skill_names: dict[str, str] = field(default_factory=dict, init=False)
    _subagent_entries: dict[str, tuple[str, BaseChatModel, str, BaseChatModel]] = field(
        default_factory=dict, init=False
    )
    _without_entry: dict[str, tuple[BaseChatModel, str | None]] = field(default_factory=dict, init=False)

    def build(self, spec: str) -> BaseChatModel | None:
        """``spec``'s model, or ``None`` when it cannot be used or one of its calls failed this run."""
        built = self._build(spec)
        return None if isinstance(built, str) or spec in self._failed else built

    def failed(self, spec: str) -> bool:
        """Whether a call on ``spec`` failed this run."""
        return spec in self._failed

    def skill_name(self, spec: str) -> str:
        """The skill whose invocation this run chose ``spec``."""
        return self._skill_names.get(spec, "the active skill")

    def for_subagent(
        self, name: str, file_spec: str, run_model: BaseChatModel, *, never_skip: bool = False
    ) -> BaseChatModel:
        """Subagent ``name``'s model: its entry, else ``file_spec``, else ``run_model``.

        With no entry, building ``file_spec`` raises as is, so the caller skips the subagent, unless ``never_skip``:
        then ``run_model`` serves and ``areport_dispatch`` reports it. An entry that cannot be built falls back to
        ``file_spec``, else ``run_model``, so it never leaves the subagent out; ``areport_dispatch`` reports it.
        """
        configured = self.entries.get(name)
        if not configured and not file_spec:
            return run_model
        if not configured and not never_skip:
            return get_model(model=file_spec)
        if not configured:
            built = build_configured_model(spec=file_spec, thinking_level=None)
            if isinstance(built, str):
                self._undispatched[name] = (file_spec, None, built)
                return run_model
            return built
        built = self._build(configured)
        if not isinstance(built, str):
            self._subagent_entries[name] = (configured, built, file_spec, run_model)
            return built
        served, served_spec = self._model_without_entry(name, file_spec, run_model)
        self._undispatched[name] = (configured, served_spec, built)
        return served

    def call_failover(self, name: str) -> list[AgentMiddleware]:
        """What subagent ``name``'s stack adds right inside its model fallback so a failing configured model hands its
        calls to the model the subagent runs on with no entry; nothing when no configured entry serves it."""
        if (entry := self._subagent_entries.get(name)) is None:
            return []
        spec, model, _, _ = entry
        return [SubagentModelFailoverMiddleware(self, name, configured_model=model, spec=spec)]

    def subagent_model_without_entry(self, name: str) -> tuple[BaseChatModel, str | None]:
        """The model subagent ``name`` runs on with no entry, and its spec (``None`` for the run's model)."""
        _, _, file_spec, run_model = self._subagent_entries[name]
        return self._model_without_entry(name, file_spec, run_model)

    async def areport_call_failure(
        self, name: str, spec: str, error: BaseException | None, *, served_spec: str | None = None
    ) -> None:
        """Stop using ``spec`` for the rest of the run, and report once that ``served_spec`` (``None``: the run's model)
        served ``name`` instead; ``error``, the failed call's, is logged with the spec's first report."""
        first = spec not in self._failed
        self._failed.add(spec)
        if self._report(name, spec, served_spec, "call_failed", exc_info=error if first else None):
            await self._aflush()
        elif first:
            logger.error("Configured %s model '%s' failed a call", self.target, spec, exc_info=error)

    async def areport_dispatch(self, name: str) -> None:
        """Report subagent ``name``'s unusable entry the first time it is dispatched."""
        if (fallback := self._undispatched.pop(name, None)) and self._report(name, *fallback):
            await self._aflush()

    async def askill_spec(self, name: str, file_spec: str | None) -> str | None:
        """The spec skill ``name`` runs on: its entry, else ``file_spec``; ``None`` keeps the run's model. The one that
        cannot be built is reported."""
        if spec := await self._askill_spec(name, file_spec):
            self._skill_names[spec] = name
        return spec

    async def _askill_spec(self, name: str, file_spec: str | None) -> str | None:
        if configured := self.entries.get(name):
            built = self._build(configured)
            if not isinstance(built, str):
                return configured
            served = self._usable_skill_file_spec(name, file_spec)
            reported = self._report(name, configured, served, built)
        elif file_spec:
            built = self._build(file_spec)
            if not isinstance(built, str):
                return file_spec
            served = None
            reported = self._report(name, file_spec, None, built)
        else:
            return None
        if reported:
            await self._aflush()
        return served

    def _usable_skill_file_spec(self, name: str, file_spec: str | None) -> str | None:
        if not file_spec:
            return None
        built = self._build(file_spec)
        if not isinstance(built, str):
            return file_spec
        logger.warning(
            "Skill %s: its file's model '%s' is unusable (%s); the run's model serves", name, file_spec, built
        )
        return None

    def _model_without_entry(
        self, name: str, file_spec: str, run_model: BaseChatModel
    ) -> tuple[BaseChatModel, str | None]:
        if name not in self._without_entry:
            self._without_entry[name] = (run_model, None)
            if file_spec:
                try:
                    self._without_entry[name] = (get_model(model=file_spec), file_spec)
                except Exception:
                    # Safe to continue: the run's model serves, and the fallback is reported.
                    logger.warning("Subagent %s: its file's model '%s' is unusable too", name, file_spec, exc_info=True)
        return self._without_entry[name]

    async def _aflush(self) -> None:
        if self.notices is not None:
            await self.notices.aflush()

    def _build(self, spec: str) -> BaseChatModel | FallbackReason:
        if spec not in self._builds:
            thinking_level = self.chain.thinking_level if self.chain else None
            self._builds[spec] = build_configured_model(spec=spec, thinking_level=thinking_level)
        return self._builds[spec]

    def _report(
        self,
        name: str,
        configured: str,
        served_spec: str | None,
        reason: FallbackReason,
        *,
        exc_info: BaseException | None = None,
    ) -> bool:
        """Log and record ``name``'s unusable entry, once per run; whether this call reported it."""
        if name in self._reported:
            return False
        self._reported.add(name)
        served_model = served_spec or (self.chain.names[0] if self.chain else "")
        logger.error(
            "Configured %s model unusable: name=%s configured_model=%s served_model=%s reason=%s",
            self.target,
            name,
            configured,
            served_model,
            reason,
            exc_info=exc_info,
        )
        if self.notices is not None:
            self.notices.record(
                ModelFallbackNotice(
                    target=self.target, name=name, configured_model=configured, served_model=served_model, reason=reason
                )
            )
        return True
