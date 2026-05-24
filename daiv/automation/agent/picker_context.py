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

from automation.agent.base import parse_model_spec
from core.models import Provider


def agent_picker_context(
    form: Any = None, *, initial_model: str = "", initial_thinking_level: str = ""
) -> dict[str, Any]:
    """Build context vars for the ``_agent_picker.html`` partial.

    Providers come from ``Provider.objects.filter(is_enabled=True)``. The model
    catalog itself is no longer rendered server-side: the Alpine component
    fetches it from ``automation:agent_models`` on first popover open. We still
    ship the enabled providers (label + slug) and the stale-model flag — both
    are cheap to compute synchronously.

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

    resolved_model = initial_model
    resolved_thinking = initial_thinking_level
    if form is not None:
        if "agent_model" in form.fields:
            resolved_model = str(form["agent_model"].value() or "") or resolved_model
        if "agent_thinking_level" in form.fields:
            resolved_thinking = str(form["agent_thinking_level"].value() or "") or resolved_thinking

    # Route through ``parse_model_spec`` so bare-name specs (``claude-opus-4-5``
    # via ``_BARE_NAME_HEURISTICS``) are flagged identically to colon-prefixed
    # ones, and so a row that resolves but is disabled also counts as stale.
    stale_model = False
    if resolved_model:
        try:
            resolved = parse_model_spec(resolved_model)
        except ValueError:
            stale_model = True
        else:
            stale_model = not resolved.row.is_enabled

    return {
        "agent_picker_providers": json.dumps(providers),
        # Kept as an empty JSON object for backwards-compatibility with templates
        # that haven't migrated to the dynamic fetch URL yet. The Alpine component
        # populates the live catalog via the ``catalogUrl`` prop instead.
        "agent_picker_models": "{}",
        "agent_picker_initial_model": resolved_model,
        "agent_picker_initial_thinking": resolved_thinking,
        "agent_picker_stale_model": stale_model,
    }
