"""Context helper for the ``_agent_picker.html`` partial.

Views that render a form with ``agent_model`` / ``agent_thinking_level`` fields call
:func:`agent_picker_context` and unpack the returned dict into the template context.
The partial expects the values keyed as ``agent_picker_*`` so multiple pickers per
page (or pickers alongside other widgets) don't collide on the bare ``providers``
/ ``models`` keys.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from automation.agent.base import parse_model_spec
from core.models import Provider, ThinkingLevelChoices
from core.site_settings import site_settings

logger = logging.getLogger("daiv.automation")


def _display_model_name(spec: str) -> str:
    """Strip ``provider:`` prefix and any ``org/`` path so the locked-pill
    rendering path shows a compact name instead of the raw spec."""
    if not spec:
        return ""
    name = spec.split(":", 1)[1] if ":" in spec else spec
    return name.rsplit("/", 1)[-1] or name


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

    # The system default seeds the picker pre-selection; gate it through the same
    # parse-and-enabled check so an admin-misconfigured default (provider disabled
    # or row removed) doesn't render a normal-looking pill pinned to an
    # unselectable spec. Empty seed leaves the picker unselected, matching the
    # "no default configured" branch in the JS. Log when we drop — this is admin
    # misconfiguration, not user input, and would otherwise be invisible (the
    # user-stored equivalent surfaces via the stale-pill UI; the default has no
    # equivalent affordance, so the log is the only operator signal).
    default_model = site_settings.agent_model_name or ""
    if default_model:
        try:
            resolved_default = parse_model_spec(default_model)
        # ``ValueError`` is the narrow, parse-only failure mode. Don't broaden:
        # DB errors on the ``.row.is_enabled`` access below should propagate
        # rather than be silenced as "picker unselected".
        except ValueError:
            logger.warning("DAIV_AGENT_MODEL_NAME=%r is unparseable; picker will render unselected.", default_model)
            default_model = ""
        else:
            if not resolved_default.row.is_enabled:
                logger.warning(
                    "DAIV_AGENT_MODEL_NAME=%r points to disabled provider %r; picker will render unselected.",
                    default_model,
                    resolved_default.row.slug,
                )
                default_model = ""

    # Mirror of the model branch for thinking effort: an env-locked admin can
    # set ``DAIV_AGENT_THINKING_LEVEL`` to anything string-y (``_parse_env_value``
    # doesn't validate enum membership), so gate the seed against the enum and
    # log on drop. No "stale" affordance for effort — it's a free-floating
    # selector, not pinned to provider state.
    default_thinking = site_settings.agent_thinking_level or ""
    if default_thinking and default_thinking not in ThinkingLevelChoices.values:
        logger.warning(
            "DAIV_AGENT_THINKING_LEVEL=%r is not a valid effort level; picker will render unselected.", default_thinking
        )
        default_thinking = ""

    return {
        "agent_picker_providers": json.dumps(providers),
        "agent_picker_initial_model": resolved_model,
        "agent_picker_initial_model_display": _display_model_name(resolved_model),
        "agent_picker_initial_thinking": resolved_thinking,
        "agent_picker_stale_model": stale_model,
        # Full ``provider:model`` spec the picker pre-selects when nothing is
        # stored. Empty when no admin default is configured OR the configured
        # default was gated above as unparseable / disabled — the JS then renders
        # the unselected "Pick a model" pill and the form refuses to submit until
        # the user chooses one.
        "agent_picker_default_model": default_model,
        # Effort level the picker pre-selects when nothing is stored. Empty
        # when unset or gated out — the JS leaves the effort dots blank.
        "agent_picker_default_thinking": default_thinking,
    }
