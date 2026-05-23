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


def agent_picker_context(form: Any) -> dict[str, Any]:
    """Build context vars for the ``_agent_picker.html`` partial.

    Providers come from ``Provider.objects.filter(is_enabled=True)``; the model
    suggestions are sourced from the :class:`ModelName` enum, grouped by provider
    slug prefix. Free-text model names are still accepted server-side via
    :func:`automation.agent.base.parse_model_spec` — the suggestions are only a
    convenience for the dropdown.

    ``form`` is a bound Django form. Initial values for ``agent_model`` and
    ``agent_thinking_level`` are read via ``form[field].value()`` when present;
    forms without those fields get empty defaults so the partial renders in its
    "Auto" state.
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

    initial_model = ""
    initial_thinking = ""
    if "agent_model" in form.fields:
        initial_model = str(form["agent_model"].value() or "")
    if "agent_thinking_level" in form.fields:
        initial_thinking = str(form["agent_thinking_level"].value() or "")

    return {
        "agent_picker_providers": json.dumps(providers),
        "agent_picker_models": json.dumps(models),
        "agent_picker_initial_model": initial_model,
        "agent_picker_initial_thinking": initial_thinking,
    }
