import warnings

from core.models import ThinkingLevelChoices
from core.site_settings import site_settings


def test_repo_config_ignores_legacy_sandbox_block():
    """A repo with a legacy `sandbox:` block in .daiv.yml still parses; the
    block is silently dropped after this redesign. Users are expected to
    recreate the configuration via the SandboxEnvironment UI."""
    from codebase.repo_config import RepositoryConfig

    yaml_data = {"default_branch": "main", "sandbox": {"base_image": "python:3.14", "memory_bytes": 2 * 2**30}}
    config = RepositoryConfig(**yaml_data)
    assert not hasattr(config, "sandbox"), "sandbox field should be removed"
    assert config.default_branch == "main"


def test_default_thinking_level_coerces_raw_site_settings_string(monkeypatch):
    """Site settings return raw strings for DB/env-set values, and pydantic skips
    validation of default_factory results — without coercion the raw string sits in
    the enum-typed field and every full model_dump() emits
    PydanticSerializationUnexpectedValue warnings."""
    from codebase.repo_config import RepositoryConfig

    monkeypatch.setattr(site_settings, "agent_thinking_level", "xhigh")
    config = RepositoryConfig()

    assert config.models.agent.thinking_level is ThinkingLevelChoices.XHIGH

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config.model_dump()
    assert not [w for w in caught if "Pydantic serializer warnings" in str(w.message)]


def test_default_thinking_level_degrades_invalid_value_to_none(monkeypatch):
    """An invalid DAIV_AGENT_THINKING_LEVEL env value must not break repository
    config loading; it degrades to None (thinking disabled)."""
    from codebase.repo_config import RepositoryConfig

    monkeypatch.setattr(site_settings, "agent_thinking_level", "banana")
    assert RepositoryConfig().models.agent.thinking_level is None

    monkeypatch.setattr(site_settings, "agent_thinking_level", "")
    assert RepositoryConfig().models.agent.thinking_level is None


def test_memory_section_defaults_enabled():
    from codebase.repo_config import RepositoryConfig

    config = RepositoryConfig()
    assert config.memory.enabled is True


def test_memory_section_can_be_disabled():
    from codebase.repo_config import RepositoryConfig

    config = RepositoryConfig(**{"memory": {"enabled": False}})
    assert config.memory.enabled is False


def test_orchestration_defaults_on():
    from codebase.repo_config import RepositoryConfig

    cfg = RepositoryConfig()
    assert cfg.orchestration.enabled is True


def test_orchestration_can_be_enabled_via_yaml():
    from codebase.repo_config import RepositoryConfig

    cfg = RepositoryConfig.model_validate({"orchestration": {"enabled": True}})
    assert cfg.orchestration.enabled is True
