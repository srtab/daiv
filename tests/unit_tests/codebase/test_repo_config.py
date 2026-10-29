from unittest.mock import patch

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


def test_the_model_and_attempt_defaults_hold_nothing_from_the_site():
    """The resolver takes an unset value from the site snapshot, so a config that copied the site in at load time
    would pin a stale value into every run the cached entry serves."""
    from codebase.repo_config import RepositoryConfig

    site = {
        "agent_model_name": "site-model",
        "agent_fallback_model_name": "site-fallback",
        "agent_thinking_level": "high",
        "diff_to_metadata_model_name": "site-d2m",
        "diff_to_metadata_fallback_model_name": "site-d2m-fallback",
        "pipeline_watch_max_attempts": 1,
    }
    with patch.multiple(site_settings, **site):
        config = RepositoryConfig()

    assert config.models.model_dump() == {
        "agent": {"model": None, "fallback_model": None, "thinking_level": None},
        "diff_to_metadata": {"model": None, "fallback_model": None},
        "skills": {},
        "subagents": {},
    }
    assert config.pipeline_watch.max_attempts is None
    assert config.model_fields_set == set()


def test_memory_section_defaults_enabled():
    from codebase.repo_config import RepositoryConfig

    config = RepositoryConfig()
    assert config.memory.enabled is True


def test_memory_section_can_be_disabled():
    from codebase.repo_config import RepositoryConfig

    config = RepositoryConfig(**{"memory": {"enabled": False}})
    assert config.memory.enabled is False


def test_pipeline_watch_defaults_leave_the_cap_to_the_site():
    from codebase.repo_config import RepositoryConfig

    config = RepositoryConfig()
    assert config.pipeline_watch.enabled is True
    assert config.pipeline_watch.max_attempts is None


def test_pipeline_watch_a_repo_can_disable_the_watch():
    from codebase.repo_config import RepositoryConfig

    config = RepositoryConfig(**{"pipeline_watch": {"enabled": False}})
    assert config.pipeline_watch.enabled is False
    assert config.pipeline_watch.max_attempts is None


def test_pipeline_watch_a_repo_can_tighten_the_attempt_cap():
    from codebase.repo_config import RepositoryConfig

    config = RepositoryConfig(**{"pipeline_watch": {"max_attempts": 1}})
    assert config.pipeline_watch.max_attempts == 1
    assert config.pipeline_watch.enabled is True


def test_skill_and_subagent_models_load_as_name_to_spec_maps():
    from codebase.repo_config import RepositoryConfig

    config = RepositoryConfig(
        models={
            "skills": {"code-review": "openrouter:anthropic/claude-sonnet-5.5", "plan": None},
            "subagents": {"cr-security": "openrouter:anthropic/claude-opus-5.5"},
        }
    )

    assert config.models.skills == {"code-review": "openrouter:anthropic/claude-sonnet-5.5", "plan": None}
    assert config.models.subagents == {"cr-security": "openrouter:anthropic/claude-opus-5.5"}


def test_an_unusable_model_spec_does_not_drop_the_rest_of_the_file(mock_repo_client):
    from codebase.repo_config import RepositoryConfig

    mock_repo_client.get_repository_file.return_value = (
        "default_branch: develop\n"
        "suggest_context_file: false\n"
        "models:\n"
        "  skills:\n"
        "    code-review: nosuchprovider:model\n"
        "  subagents:\n"
        "    cr-tests: nosuchprovider:model\n"
    )
    repo_id = "group/bad-model-spec"
    try:
        config = RepositoryConfig.get_config(repo_id)
    finally:
        RepositoryConfig.invalidate_cache(repo_id)

    assert (config.default_branch, config.suggest_context_file) == ("develop", False)
    assert config.models.skills == {"code-review": "nosuchprovider:model"}
    assert config.models.subagents == {"cr-tests": "nosuchprovider:model"}
