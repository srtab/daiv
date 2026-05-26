from __future__ import annotations

from django import template

from automation.agent.display import MODEL_NAME_MAX_LEN, display_model_name, display_thinking_level

register = template.Library()


@register.inclusion_tag("automation/_agent_model_pill.html")
def agent_model_pill(model: str, thinking_level: str = "") -> dict:
    """Read-only ``meta-pill`` for an agent model spec; renders nothing when ``model`` is empty."""
    if not model:
        return {}
    effort = display_thinking_level(thinking_level)
    return {
        "name": display_model_name(model, max_len=MODEL_NAME_MAX_LEN),
        "effort": effort,
        "title": f"{model} · {effort}" if effort else model,
    }
