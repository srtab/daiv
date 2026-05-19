def test_repo_config_ignores_legacy_sandbox_block():
    """A repo with a legacy `sandbox:` block in .daiv.yml still parses; the
    block is silently dropped after this redesign. Users are expected to
    recreate the configuration via the SandboxEnvironment UI."""
    from codebase.repo_config import RepositoryConfig

    yaml_data = {"default_branch": "main", "sandbox": {"base_image": "python:3.14", "memory_bytes": 2 * 2**30}}
    config = RepositoryConfig(**yaml_data)
    assert not hasattr(config, "sandbox"), "sandbox field should be removed"
    assert config.default_branch == "main"
