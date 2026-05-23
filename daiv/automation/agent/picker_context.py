"""Context helper for the ``_agent_picker.html`` partial.

Views that render a form with ``agent_model`` / ``agent_thinking_level`` fields call
:func:`agent_picker_context` and unpack the returned dict into the template context.
The partial expects the values keyed as ``agent_picker_*`` so multiple pickers per
page (or pickers alongside other widgets) don't collide on the bare ``providers``
/ ``models`` keys.
"""

from __future__ import annotations

import json
from typing import Any

from automation.agent.constants import ModelName
from core.models import Provider


def agent_picker_context(
    form: Any = None, *, initial_model: str = "", initial_thinking_level: str = ""
) -> dict[str, Any]:
    """Build context vars for the ``_agent_picker.html`` partial.

    Providers come from ``Provider.objects.filter(is_enabled=True)``; the model
    suggestions are sourced from the :class:`ModelName` enum, grouped by provider
    slug prefix. Free-text model names are still accepted server-side via
    :func:`automation.agent.base.parse_model_spec` — the suggestions are only a
    convenience for the dropdown.

    ``form`` (optional) is a bound Django form. When provided, initial values for
    ``agent_model`` and ``agent_thinking_level`` are read via ``form[field].value()``
    when those fields exist; missing fields fall back to the explicit ``initial_*``
    kwargs. Surfaces without a Django form (e.g. the chat composer) pass the initials
    directly, leaving ``form`` as ``None``.

    When ``initial_model`` references a provider that is no longer enabled (or that
    has no enabled Provider row at all), ``agent_picker_stale_model`` is ``True`` so
    the partial can render a "no longer available" hint instead of showing a
    silently-unselectable value.
    """
    providers = [
        {"slug": row.slug, "label": row.display_name or row.slug.replace("_", " ").title()}
        for row in Provider.objects.filter(is_enabled=True).order_by("sort_order", "slug")
    ]
    enabled_slugs = {p["slug"] for p in providers}
    models: dict[str, list[str]] = {slug: [] for slug in enabled_slugs}
    for spec in ModelName:
        prefix, name = spec.value.split(":", 1)
        if prefix in models:
            models[prefix].append(name)

    resolved_model = initial_model
    resolved_thinking = initial_thinking_level
    if form is not None:
        if "agent_model" in form.fields:
            resolved_model = str(form["agent_model"].value() or "") or resolved_model
        if "agent_thinking_level" in form.fields:
            resolved_thinking = str(form["agent_thinking_level"].value() or "") or resolved_thinking

    stale_model = False
    if resolved_model and ":" in resolved_model:
        prefix = resolved_model.split(":", 1)[0]
        stale_model = prefix not in enabled_slugs

    return {
        "agent_picker_providers": json.dumps(providers),
        "agent_picker_models": json.dumps(models),
        "agent_picker_initial_model": resolved_model,
        "agent_picker_initial_thinking": resolved_thinking,
        "agent_picker_stale_model": stale_model,
    }
