from unittest.mock import patch

from codebase.repo_config import CONFIGURATION_CACHE_KEY_PREFIX, CONFIGURATION_CACHE_TIMEOUT, RepositoryConfig


class RepositoryConfigTest:
    @patch("codebase.repo_config.cache")
    def test_get_config_from_cache(self, mock_cache):
        repo_id = "test_repo"
        cached_config = {
            "default_branch": "main",
            "code_review": {"enabled": True},
            "issue_addressing": {"enabled": True},
            "quick_actions": {"enabled": True},
            "extend_exclude_patterns": ["tests/"],
        }
        mock_cache.get.return_value = cached_config

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.code_review.enabled is True
        assert config.issue_addressing.enabled is True
        assert config.quick_actions.enabled is True
        assert config.extend_exclude_patterns == ["tests/"]
        mock_cache.get.assert_called_once_with(f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}")

    @patch("codebase.repo_config.cache")
    def test_get_config_from_repo(self, mock_cache, mock_repo_client):
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_client.get_repository.return_value.default_branch = "main"
        mock_repo_client.get_repository_file.return_value = """
        default_branch: main
        code_review:
          enabled: true
        issue_addressing:
          enabled: true
        quick_actions:
          enabled: true
        extend_exclude_patterns:
          - tests/
        """

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.code_review.enabled is True
        assert config.issue_addressing.enabled is True
        assert config.quick_actions.enabled is True
        assert config.extend_exclude_patterns == ["tests/"]
        mock_cache.set.assert_called_once_with(
            f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}", config.model_dump(), CONFIGURATION_CACHE_TIMEOUT
        )

    @patch("codebase.repo_config.cache")
    def test_get_config_with_default_values(self, mock_cache, mock_repo_client):
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_client.get_repository.return_value.default_branch = "main"
        mock_repo_client.get_repository_file.return_value = None

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.code_review.enabled is True
        assert config.issue_addressing.enabled is True
        assert config.quick_actions.enabled is True
        assert config.extend_exclude_patterns == []
        mock_cache.set.assert_called_once_with(
            f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}", config.model_dump(), CONFIGURATION_CACHE_TIMEOUT
        )

    @patch("codebase.repo_config.cache")
    def test_invalidate_cache(self, mock_cache):
        repo_id = "test_repo"
        RepositoryConfig.invalidate_cache(repo_id)
        mock_cache.delete.assert_called_once_with(f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}")

    @patch("codebase.repo_config.cache")
    def test_get_config_with_invalid_yaml(self, mock_cache, mock_repo_client):
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_client.get_repository.return_value.default_branch = "main"
        mock_repo_client.get_repository_file.return_value = "invalid_yaml: [unclosed_list"

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.code_review.enabled is True
        assert config.issue_addressing.enabled is True
        assert config.quick_actions.enabled is True
        assert config.extend_exclude_patterns == []
        mock_cache.set.assert_called_once_with(
            f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}", config.model_dump(), CONFIGURATION_CACHE_TIMEOUT
        )

    @patch("codebase.repo_config.cache")
    def test_get_config_with_partial_yaml(self, mock_cache, mock_repo_client):
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_client.get_repository.return_value.default_branch = "main"
        mock_repo_client.get_repository_file.return_value = """
        default_branch: main
        """

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.code_review.enabled is True
        assert config.issue_addressing.enabled is True
        assert config.quick_actions.enabled is True
        assert config.extend_exclude_patterns == []
        mock_cache.set.assert_called_once_with(
            f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}", config.model_dump(), CONFIGURATION_CACHE_TIMEOUT
        )

    @patch("codebase.repo_config.cache")
    def test_get_config_with_models_section(self, mock_cache, mock_repo_client):
        """Test that models configuration is parsed correctly from YAML."""
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_client.get_repository.return_value.default_branch = "main"
        mock_repo_client.get_repository_file.return_value = """
        models:
          agent:
            model: "openrouter:anthropic/claude-haiku-4.5"
            thinking_level: "low"
          pr_describer:
            model: "openrouter:openai/gpt-4.1-mini"
        """

        config = RepositoryConfig.get_config(repo_id)

        assert config.models.agent.model == "openrouter:anthropic/claude-haiku-4.5"
        assert config.models.agent.thinking_level == "low"
        assert config.models.pr_describer.model == "openrouter:openai/gpt-4.1-mini"

    @patch("codebase.repo_config.cache")
    def test_get_config_with_partial_models_section(self, mock_cache, mock_repo_client):
        """Test that partial models configuration works correctly."""
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_client.get_repository.return_value.default_branch = "main"
        mock_repo_client.get_repository_file.return_value = """
        models:
          agent:
            model: "openrouter:anthropic/claude-haiku-4.5"
        """

        config = RepositoryConfig.get_config(repo_id)

        assert config.models.agent.model == "openrouter:anthropic/claude-haiku-4.5"
        assert config.models.agent.thinking_level is not None
