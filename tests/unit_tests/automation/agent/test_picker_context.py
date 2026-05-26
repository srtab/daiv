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


def test_stale_model_flag_set_for_bare_name_with_disabled_heuristic_provider(providers):
    """``parse_model_spec`` accepts bare names via ``_BARE_NAME_HEURISTICS``; if the
    target slug exists but is disabled (the ``anthropic`` fixture state), the bare
    name must still be flagged stale or the picker silently renders a dead value."""
    from core.models import Provider

    Provider.invalidate_cache()
    ctx = agent_picker_context(initial_model="claude-opus-4-5")
    assert ctx["agent_picker_initial_model"] == "claude-opus-4-5"
    assert ctx["agent_picker_stale_model"] is True


def test_stale_model_flag_set_for_existing_but_disabled_provider(providers):
    """A row that resolves via ``parse_model_spec`` but has ``is_enabled=False``
    must be flagged stale — the prior implementation only checked ``prefix not in
    enabled_slugs`` which already covered this, but the new ``parse_model_spec``
    routing must preserve the behaviour."""
    from core.models import Provider

    Provider.invalidate_cache()
    ctx = agent_picker_context(initial_model="anthropic:claude-sonnet-4.6")
    assert ctx["agent_picker_stale_model"] is True


def test_stale_model_flag_set_for_unparseable_spec(providers):
    from core.models import Provider

    Provider.invalidate_cache()
    ctx = agent_picker_context(initial_model="totally:not-a-real-provider")
    assert ctx["agent_picker_stale_model"] is True


def test_default_model_is_full_spec(providers, monkeypatch):
    """The picker pre-selects the system default — Alpine needs the full
    ``provider:model`` spec to seed ``selectedProvider`` / ``modelName``."""
    from core.site_settings import site_settings

    monkeypatch.setattr(site_settings, "agent_model_name", "openrouter:anthropic/claude-sonnet-4.6")
    ctx = agent_picker_context()
    assert ctx["agent_picker_default_model"] == "openrouter:anthropic/claude-sonnet-4.6"


def test_default_model_empty_when_unset(providers, monkeypatch):
    from core.site_settings import site_settings

    monkeypatch.setattr(site_settings, "agent_model_name", "")
    ctx = agent_picker_context()
    assert ctx["agent_picker_default_model"] == ""


def test_stale_initial_does_not_suppress_default_seed(providers, monkeypatch):
    """Stale-detection and default-gating are independent code paths. A stale
    user-stored spec (rendered with the ``!`` marker) must NOT also swap the
    seed in — the stale pill itself is the migration prompt, and silently
    pre-selecting a different model would hide it. Regression guard against
    a future refactor that conflates the two."""
    from core.models import Provider
    from core.site_settings import site_settings

    # ``anthropic:...`` is stale per the fixture (anthropic row is disabled).
    Provider.invalidate_cache()
    monkeypatch.setattr(site_settings, "agent_model_name", "openrouter:anthropic/claude-sonnet-4.6")
    ctx = agent_picker_context(initial_model="anthropic:claude-sonnet-4.6")
    assert ctx["agent_picker_stale_model"] is True
    assert ctx["agent_picker_default_model"] == "openrouter:anthropic/claude-sonnet-4.6"
    assert ctx["agent_picker_initial_model"] == "anthropic:claude-sonnet-4.6"


def test_default_model_dropped_when_provider_disabled(providers, monkeypatch):
    """An admin-misconfigured default (provider disabled after the value was set)
    must not pre-select an unselectable spec — the seed is dropped so the picker
    falls back to the unselected ``Auto`` pill instead of silently submitting
    a value the server will reject."""
    from core.models import Provider
    from core.site_settings import site_settings

    Provider.invalidate_cache()
    monkeypatch.setattr(site_settings, "agent_model_name", "anthropic:claude-sonnet-4.6")
    ctx = agent_picker_context()
    assert ctx["agent_picker_default_model"] == ""


def test_default_model_dropped_when_unparseable(providers, monkeypatch):
    from core.models import Provider
    from core.site_settings import site_settings

    Provider.invalidate_cache()
    monkeypatch.setattr(site_settings, "agent_model_name", "totally:not-a-real-provider")
    ctx = agent_picker_context()
    assert ctx["agent_picker_default_model"] == ""


def test_default_thinking_seeded_from_site_settings(providers, monkeypatch):
    """The picker pre-selects the system default effort, mirroring the model branch."""
    from core.site_settings import site_settings

    monkeypatch.setattr(site_settings, "agent_thinking_level", "high")
    ctx = agent_picker_context()
    assert ctx["agent_picker_default_thinking"] == "high"


def test_default_thinking_empty_when_unset(providers, monkeypatch):
    from core.site_settings import site_settings

    monkeypatch.setattr(site_settings, "agent_thinking_level", "")
    ctx = agent_picker_context()
    assert ctx["agent_picker_default_thinking"] == ""


def test_default_thinking_dropped_when_invalid(providers, monkeypatch):
    """Env-locked admin can stuff anything into ``DAIV_AGENT_THINKING_LEVEL``;
    a non-enum value must be dropped, not pre-selected as an unselectable string."""
    from core.site_settings import site_settings

    monkeypatch.setattr(site_settings, "agent_thinking_level", "ludicrous")
    ctx = agent_picker_context()
    assert ctx["agent_picker_default_thinking"] == ""


def test_initial_thinking_does_not_suppress_default_seed(providers, monkeypatch):
    """Stored thinking value and system default are independent — the default
    must still flow through so a different surface rendering the same partial
    sees both."""
    from core.site_settings import site_settings

    monkeypatch.setattr(site_settings, "agent_thinking_level", "high")
    ctx = agent_picker_context(initial_thinking_level="low")
    assert ctx["agent_picker_initial_thinking"] == "low"
    assert ctx["agent_picker_default_thinking"] == "high"


def test_initial_model_display_strips_provider_and_org_prefix(providers):
    """The locked pill renders ``initial_model_display`` — names are normalised
    by stripping ``provider:`` and any ``org/`` path."""
    ctx = agent_picker_context(initial_model="openrouter:anthropic/claude-haiku-4.5")
    assert ctx["agent_picker_initial_model_display"] == "claude-haiku-4.5"


def test_initial_model_display_strips_colon_without_org(providers):
    """Anthropic-direct specs are ``provider:model`` with no ``org/`` segment —
    the helper must still strip the prefix without choking on the missing slash."""
    ctx = agent_picker_context(initial_model="anthropic:claude-opus-4-6")
    assert ctx["agent_picker_initial_model_display"] == "claude-opus-4-6"


def test_initial_model_display_passes_bare_name_through(providers):
    """Bare-name specs (no colon, no slash) are valid via ``_BARE_NAME_HEURISTICS``
    — the locked pill should render the name unchanged for these."""
    # ``claude-opus-4-5`` resolves via the bare-name heuristic to the disabled
    # ``anthropic`` row in the fixture; stale-flag handling is exercised in a
    # separate test. Here we only care that the display passes through verbatim.
    ctx = agent_picker_context(initial_model="claude-opus-4-5")
    assert ctx["agent_picker_initial_model_display"] == "claude-opus-4-5"


def test_initial_model_display_empty_when_no_initial(providers):
    ctx = agent_picker_context()
    assert ctx["agent_picker_initial_model_display"] == ""


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("openrouter:anthropic/claude-haiku-4.5", "claude-haiku-4.5"),
        ("anthropic:claude-opus-4-6", "claude-opus-4-6"),
        ("claude-opus-4-5", "claude-opus-4-5"),
        ("openrouter:meta-llama/llama-3.3-70b-instruct", "llama-3.3-70b-instruct"),
        ("", ""),
        # Empty model name after the colon — trailing-slash fallback keeps both sides
        # returning "" rather than the raw "provider:" prefix.
        ("provider:", ""),
        # Trailing slash with empty leaf — the `or name` fallback in the helper means
        # we return the un-split remainder ("org/") rather than "", and the JS mirror
        # must do the same.
        ("provider:org/", "org/"),
    ],
)
def test_display_model_name_normalisation(spec, expected):
    """Pin the exact spec→display normalisation. The JS picker mirrors this in
    ``automation/static/automation/js/agent-picker.js`` (``shortenModel`` and the
    inline ``modelName.split('/').pop()`` in ``pillLabel``) so the locked composer
    pill — driven by the picker's ``daiv:agent-changed`` event — shows the same
    name as a server-rendered persisted thread. Update both sides together when
    this table changes."""
    from automation.agent.display import display_model_name

    assert display_model_name(spec) == expected


def test_display_model_name_truncates_when_max_len_set():
    """``max_len`` is opt-in; consumers that render in tight layouts (gallery, pill)
    pass ``MODEL_NAME_MAX_LEN`` to share the cap. Without the kwarg, the full
    normalised name flows through so callers can format their own."""
    from automation.agent.display import MODEL_NAME_MAX_LEN, display_model_name

    long_spec = "provider:vendor/" + "x" * 50
    assert display_model_name(long_spec) == "x" * 50
    assert display_model_name(long_spec, max_len=MODEL_NAME_MAX_LEN) == "x" * MODEL_NAME_MAX_LEN


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("minimal", "Minimal"),
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("xhigh", "Extra high"),
        ("", ""),
        ("bogus", ""),
    ],
)
def test_display_thinking_level(level, expected):
    """Translation-aware label lookup. Unknown values (renamed enum members,
    legacy data) must fall back to empty rather than leaking the raw key."""
    from automation.agent.display import display_thinking_level

    assert display_thinking_level(level) == expected


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("minimal", 1),
        ("low", 2),
        ("medium", 3),
        ("high", 4),
        ("xhigh", 5),
        # Empty or unknown level → 0 dots (the partial hides the block via x-show / the
        # ``{% elif initial_effort_dots %}`` gate; both branches treat 0 as "no meter").
        ("", 0),
        ("absurd", 0),
    ],
)
def test_effort_dots_for_level_normalisation(level, expected):
    """Pin the level→dots mapping. The JS picker mirrors this in
    ``automation/static/automation/js/agent-picker.js`` (``EFFORT_LEVELS`` + ``dotsFor``)
    so the locked composer pill — driven by the picker's ``daiv:agent-changed`` event —
    lights the same dots as a server-rendered persisted thread. Update both sides
    together when ``ThinkingLevelChoices`` gains or loses a level."""
    from core.models import ThinkingLevelChoices

    assert ThinkingLevelChoices.dots_for(level) == expected


@pytest.mark.parametrize(("dots", "expected_on_count"), [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])
def test_locked_partial_renders_static_effort_dots(providers, dots, expected_on_count):
    """The locked-pill static branch (no ``dynamic_effort_dots_expr``) emits exactly
    five dot spans and lights the leading ``initial_effort_dots`` of them. Used by
    any caller that omits the Alpine expression — typically the env-locked settings
    widget; the chat composer takes the Alpine branch instead."""
    from django.template.loader import render_to_string

    html = render_to_string(
        "automation/_agent_picker.html",
        {
            "disabled": True,
            "initial_agent_model": "openrouter:anthropic/claude-haiku-4.5",
            "initial_model_display": "claude-haiku-4.5",
            "initial_effort_dots": dots,
            "providers": "[]",
        },
    )
    # Total dot spans is always either 5 (effort > 0 → block rendered) or 0 (block omitted).
    expected_dot_spans = 5 if dots else 0
    assert html.count('class="agent-pill__effort-dot') == expected_dot_spans
    # Only the leading ``initial_effort_dots`` of those five get the ``--on`` suffix; the
    # remainder render with the bare base class.
    assert html.count("agent-pill__effort-dot--on") == expected_on_count


def test_locked_partial_renders_effort_dots_alongside_stale_marker(providers):
    """A stale (provider-disabled) locked pill must still show the effort the user picked
    — only the *model* is no longer valid, the effort selection is still informative.
    The stale marker sits at the trailing slot replacing the lock icon; effort dots stay
    in their slot between the name and the trailing diagnostic."""
    from django.template.loader import render_to_string

    html = render_to_string(
        "automation/_agent_picker.html",
        {
            "disabled": True,
            "stale": True,
            "initial_agent_model": "removed:org/model",
            "initial_model_display": "model",
            "initial_effort_dots": 3,
            "providers": "[]",
        },
    )
    assert html.count("agent-pill__effort-dot--on") == 3
    assert "agent-pill__stale-marker" in html
    # Stale variant suppresses the cpu-chip icon (the model is unreachable) but keeps the
    # effort meter — the effort row is independent of model validity.
    assert "agent-pill__icon" not in html
