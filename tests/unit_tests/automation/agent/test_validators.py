import pytest

from automation.agent.validators import AgentOverrideError, validate_agent_override
from core.models import Provider, ProviderType, ThinkingLevelChoices


@pytest.fixture
def openrouter_provider(db):
    Provider.objects.filter(slug="openrouter").delete()
    return Provider.objects.create(
        slug="openrouter", provider_type=ProviderType.OPENROUTER, api_key="sk-test", is_enabled=True
    )


def test_returns_empty_strings_for_no_overrides(openrouter_provider):
    model, level = validate_agent_override("", "")
    assert model == ""
    assert level == ""


def test_normalises_none_inputs(openrouter_provider):
    model, level = validate_agent_override(None, None)
    assert model == ""
    assert level == ""


def test_accepts_valid_pair(openrouter_provider):
    model, level = validate_agent_override("openrouter:anthropic/claude-sonnet-4.6", ThinkingLevelChoices.HIGH)
    assert model == "openrouter:anthropic/claude-sonnet-4.6"
    assert level == ThinkingLevelChoices.HIGH


def test_rejects_unknown_provider_prefix(db):
    with pytest.raises(AgentOverrideError) as err:
        validate_agent_override("nopesuch:model-x", "")
    assert "Unknown provider prefix 'nopesuch'" in str(err.value)


def test_rejects_empty_model_after_prefix(openrouter_provider):
    with pytest.raises(AgentOverrideError) as err:
        validate_agent_override("openrouter:", "")
    assert "Empty model name" in str(err.value)


def test_rejects_invalid_thinking_level(openrouter_provider):
    with pytest.raises(AgentOverrideError) as err:
        validate_agent_override("openrouter:anthropic/claude-sonnet-4.6", "extreme")
    assert "thinking level" in str(err.value).lower()


def test_thinking_level_alone_is_allowed(openrouter_provider):
    model, level = validate_agent_override("", ThinkingLevelChoices.LOW)
    assert model == ""
    assert level == ThinkingLevelChoices.LOW


def test_rejects_disabled_provider(db):
    """A Provider row that exists but has ``is_enabled=False`` must be rejected at
    submit time, otherwise the deep ``RuntimeError`` inside ``BaseAgent.get_model_kwargs``
    fires mid-run with no actionable signal at the boundary."""
    Provider.objects.filter(slug="openrouter").delete()
    Provider.objects.create(
        slug="openrouter", provider_type=ProviderType.OPENROUTER, api_key="sk-test", is_enabled=False
    )
    Provider.invalidate_cache()
    try:
        with pytest.raises(AgentOverrideError) as err:
            validate_agent_override("openrouter:anthropic/claude-haiku-4.5", "")
        assert "disabled" in str(err.value).lower()
    finally:
        Provider.invalidate_cache()
