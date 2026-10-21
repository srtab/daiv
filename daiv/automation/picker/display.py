"""Display helpers for agent model spec and thinking level."""

from __future__ import annotations

from core.models import ThinkingLevelChoices

MODEL_NAME_MAX_LEN = 24


def display_model_name(spec: str, *, max_len: int | None = None) -> str:
    """Strip ``provider:`` and ``org/``: ``openrouter:anthropic/claude-haiku-4.5`` → ``claude-haiku-4.5``."""
    if not spec:
        return ""
    name = spec.split(":", 1)[1] if ":" in spec else spec
    name = name.rsplit("/", 1)[-1] or name
    return name[:max_len]


_THINKING_LABELS = dict(ThinkingLevelChoices.choices)


def display_thinking_level(level: str) -> str:
    """Translated label for a thinking level (``"high"`` → ``"High"``)."""
    return str(_THINKING_LABELS.get(level, ""))
