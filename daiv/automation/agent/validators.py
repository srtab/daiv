from __future__ import annotations

from automation.agent.base import parse_model_spec
from core.models import ThinkingLevelChoices


class AgentOverrideError(ValueError):
    """Raised when the user-supplied agent override is invalid.

    Carries the same message text across API/MCP/form surfaces so callers can
    surface it verbatim without per-layer rephrasing.
    """


_VALID_LEVELS = frozenset(ThinkingLevelChoices.values)


def validate_agent_override(agent_model: str | None, agent_thinking_level: str | None) -> tuple[str, str]:
    """Validate the per-run model override pair.

    Returns the normalised ``(agent_model, agent_thinking_level)`` strings: empty
    string when the input is ``None`` or empty. ``agent_model`` is parsed against
    the live ``Provider`` table; ``agent_thinking_level`` is checked against
    ``ThinkingLevelChoices``. Either field may be empty independently — setting
    thinking without a model overrides effort on the auto-resolved model.
    """
    model = (agent_model or "").strip()
    level = (agent_thinking_level or "").strip()

    if model:
        try:
            parse_model_spec(model)
        except ValueError as err:
            raise AgentOverrideError(str(err)) from err

    if level and level not in _VALID_LEVELS:
        raise AgentOverrideError(f"Invalid thinking level '{level}'. Valid: {', '.join(sorted(_VALID_LEVELS))}.")

    return model, level
