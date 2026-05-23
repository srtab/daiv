"""Tests for ``automation.agent.picker_context.agent_picker_context``.

We don't exercise the Alpine component or the Django template here — the partial
is smoke-tested via the run composer / chat composer integration tests added in
later tasks. These tests only cover the Python helper that produces context vars.
"""

import json

import pytest

from automation.agent.picker_context import agent_picker_context
from core.models import Provider, ProviderType


class _StubBoundField:
    def __init__(self, value):
        self._value = value

    def value(self):
        return self._value


class _StubForm:
    """Minimal duck-typed stand-in for a bound Django form.

    The helper only touches ``form.fields`` (membership check) and
    ``form[field].value()`` (returning the bound or initial value), so a real
    ``Form`` is overkill here.
    """

    def __init__(self, fields, values):
        self.fields = dict.fromkeys(fields)
        self._values = values

    def __getitem__(self, key):
        return _StubBoundField(self._values.get(key, ""))


@pytest.fixture
def providers(db):
    """Reset the two seeded providers we care about to a known state.

    Seed migrations create ``anthropic``, ``openai``, ``openrouter`` and
    ``google_genai`` as ``is_locked=True``. We can't ``.delete()`` them (model-level
    guard), but ``QuerySet.delete()`` bypasses that — use it to reset and
    recreate with the flags this test cares about. Other seeded rows keep their
    defaults; assertions are scoped to the slugs we explicitly created.
    """
    Provider.objects.filter(slug__in=["openrouter", "anthropic"]).delete()
    Provider.objects.create(
        slug="openrouter",
        display_name="OpenRouter",
        provider_type=ProviderType.OPENROUTER,
        api_key="sk-1",
        is_enabled=True,
    )
    Provider.objects.create(
        slug="anthropic",
        display_name="Anthropic",
        provider_type=ProviderType.ANTHROPIC,
        api_key="sk-2",
        is_enabled=False,
    )


def test_excludes_disabled_providers(providers):
    form = _StubForm(["agent_model", "agent_thinking_level"], {})
    ctx = agent_picker_context(form)
    slugs = [p["slug"] for p in json.loads(ctx["agent_picker_providers"])]
    assert "openrouter" in slugs
    assert "anthropic" not in slugs  # disabled — must be filtered out


def test_initial_values_round_trip(providers):
    form = _StubForm(
        ["agent_model", "agent_thinking_level"],
        {"agent_model": "openrouter:anthropic/claude-haiku-4.5", "agent_thinking_level": "low"},
    )
    ctx = agent_picker_context(form)
    assert ctx["agent_picker_initial_model"] == "openrouter:anthropic/claude-haiku-4.5"
    assert ctx["agent_picker_initial_thinking"] == "low"


def test_missing_form_fields_default_to_empty(providers):
    form = _StubForm([], {})
    ctx = agent_picker_context(form)
    assert ctx["agent_picker_initial_model"] == ""
    assert ctx["agent_picker_initial_thinking"] == ""


def test_none_field_values_normalised_to_empty(providers):
    # Unbound forms return ``None`` for unset values; the helper must coerce that
    # to ``""`` so the template's ``escapejs`` doesn't render the literal ``None``.
    form = _StubForm(["agent_model", "agent_thinking_level"], {"agent_model": None, "agent_thinking_level": None})
    ctx = agent_picker_context(form)
    assert ctx["agent_picker_initial_model"] == ""
    assert ctx["agent_picker_initial_thinking"] == ""


def test_models_grouped_by_provider(providers):
    form = _StubForm([], {})
    ctx = agent_picker_context(form)
    models = json.loads(ctx["agent_picker_models"])
    # ``ModelName`` enum is openrouter-only at time of writing — disabling
    # anthropic should leave it absent from the models dict entirely.
    assert "openrouter" in models
    assert len(models["openrouter"]) > 0
    assert "anthropic" not in models


def test_provider_uses_display_name_as_label(providers):
    form = _StubForm([], {})
    ctx = agent_picker_context(form)
    by_slug = {p["slug"]: p["label"] for p in json.loads(ctx["agent_picker_providers"])}
    assert by_slug["openrouter"] == "OpenRouter"


def test_explicit_initials_when_form_is_none(providers):
    """Form-less surfaces (e.g. the chat composer) pass initials directly."""
    ctx = agent_picker_context(initial_model="openrouter:anthropic/claude-haiku-4.5", initial_thinking_level="high")
    assert ctx["agent_picker_initial_model"] == "openrouter:anthropic/claude-haiku-4.5"
    assert ctx["agent_picker_initial_thinking"] == "high"


def test_form_value_overrides_explicit_initials(providers):
    """When both are supplied the bound form wins — the form is the source of truth
    on form-driven surfaces, kwargs only seed the form-less path."""
    form = _StubForm(
        ["agent_model", "agent_thinking_level"],
        {"agent_model": "openrouter:anthropic/claude-sonnet-4.5", "agent_thinking_level": "low"},
    )
    ctx = agent_picker_context(form, initial_model="ignored:spec", initial_thinking_level="medium")
    assert ctx["agent_picker_initial_model"] == "openrouter:anthropic/claude-sonnet-4.5"
    assert ctx["agent_picker_initial_thinking"] == "low"


def test_stale_model_flag_set_when_provider_disabled(providers):
    """A persisted spec whose provider was disabled after creation must be flagged
    stale so the partial can warn instead of silently rendering an unselectable value."""
    ctx = agent_picker_context(initial_model="anthropic:claude-opus-4-6")
    assert ctx["agent_picker_initial_model"] == "anthropic:claude-opus-4-6"
    assert ctx["agent_picker_stale_model"] is True


def test_stale_model_flag_false_for_enabled_provider(providers):
    ctx = agent_picker_context(initial_model="openrouter:anthropic/claude-haiku-4.5")
    assert ctx["agent_picker_stale_model"] is False


def test_stale_model_flag_false_when_no_initial_model(providers):
    ctx = agent_picker_context()
    assert ctx["agent_picker_stale_model"] is False
