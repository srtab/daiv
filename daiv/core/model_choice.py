from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from core.models import THINKING_OFF, ThinkingLevelChoices

logger = logging.getLogger("daiv.core")

VALID_LEVELS = frozenset(ThinkingLevelChoices.values) | {THINKING_OFF}


@dataclass(frozen=True)
class ModelChoice:
    """A model and/or thinking level for a skill or subagent; an unset field is ``""``."""

    model: str = ""
    thinking_level: str = ""

    def __bool__(self) -> bool:
        return bool(self.model or self.thinking_level)

    def over(self, fallback: ModelChoice) -> ModelChoice:
        return ModelChoice(
            model=self.model or fallback.model, thinking_level=self.thinking_level or fallback.thinking_level
        )

    def stored(self) -> str | dict[str, str]:
        if not self.thinking_level:
            return self.model
        data = {"model": self.model} if self.model else {}
        return {**data, "thinking_level": self.thinking_level}


EMPTY_CHOICE = ModelChoice()


def parse_model_choice(value: object, *, source: str, log_level: int = logging.ERROR) -> ModelChoice:
    """Parse a string (model only) or mapping (``model``/``thinking_level``) into a ModelChoice.

    Invalid parts are dropped and logged at `log_level`; the valid parts still apply.

    Args:
        value: The raw value read from `source`.
        source: Where the value came from, named in log lines.
        log_level: Level for every "dropped" log line.
    """
    if isinstance(value, ModelChoice):
        return value
    if value is None:
        return EMPTY_CHOICE
    if isinstance(value, str):
        return ModelChoice(model=value.strip())
    if not isinstance(value, Mapping):
        logger.log(log_level, "Ignoring a model choice from %s: expected a string or mapping", source)
        return EMPTY_CHOICE

    model = value.get("model")
    if model is None:
        model = ""
    elif isinstance(model, str):
        model = model.strip()
    else:
        logger.log(log_level, "Ignoring a model from %s: expected a string", source)
        model = ""

    level = value.get("thinking_level")
    level = "" if level is None else str(level).strip().lower()
    if level == "false":
        level = THINKING_OFF
    if level and level not in VALID_LEVELS:
        logger.log(
            log_level,
            "Ignoring an unknown thinking_level from %s: expected one of %s",
            source,
            ", ".join(sorted(VALID_LEVELS)),
        )
        level = ""
    return ModelChoice(model=model, thinking_level=level)


def parse_model_choices(value: object, *, source: str) -> dict[str, ModelChoice]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        logger.error("Ignoring model choices from %s: expected a mapping", source)
        return {}
    choices = {}
    for name, raw in value.items():
        if not isinstance(name, str):
            logger.error("Ignoring a model choice from %s whose name is not a string", source)
            continue
        # Logs name the source only: a map read from a Docker secret must not reach the log.
        if choice := parse_model_choice(raw, source=source):
            choices[name] = choice
    return choices


def merge_model_choices(over: Mapping[str, ModelChoice], under: Mapping[str, ModelChoice]) -> dict[str, ModelChoice]:
    return {name: over.get(name, EMPTY_CHOICE).over(under.get(name, EMPTY_CHOICE)) for name in {*over, *under}}
