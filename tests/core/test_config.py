from unittest.mock import Mock, patch

from core.config import CONFIGURATION_CACHE_KEY_PREFIX, CONFIGURATION_CACHE_TIMEOUT, RepositoryConfig


class TestRepositoryConfig:
    @patch("daiv.core.config.cache")
    @patch("daiv.core.config.RepoClient")
    def test_get_config_from_cache(self, mock_repo_client, mock_cache):
        repo_id = "test_repo"
        cached_config = {
            "default_branch": "main",
            "repository_description": "Test repository",
            "features": {
                "auto_address_review_enabled": True,
                "auto_address_issues_enabled": True,
                "autofix_pipeline_enabled": True,
            },
            "extend_exclude_patterns": ["tests/"],
            "branch_name_convention": "always start with 'daiv/' followed by a short description.",
        }
        mock_cache.get.return_value = cached_config

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.repository_description == "Test repository"
        assert config.features.auto_address_review_enabled is True
        assert config.extend_exclude_patterns == ["tests/"]
        assert config.branch_name_convention == "always start with 'daiv/' followed by a short description."
        mock_cache.get.assert_called_once_with(f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}")

    @patch("daiv.core.config.cache")
    @patch("daiv.core.config.RepoClient")
    def test_get_config_from_repo(self, mock_repo_client, mock_cache):
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_instance = Mock()
        mock_repo_instance.get_repository.return_value.default_branch = "main"
        mock_repo_instance.get_repository_file.return_value = """
        default_branch: main
        repository_description: Test repository
        features:
          auto_address_review_enabled: true
          auto_address_issues_enabled: true
          autofix_pipeline_enabled: true
        extend_exclude_patterns:
          - tests/
        branch_name_convention: always start with 'daiv/' followed by a short description.
        """
        mock_repo_client.create_instance.return_value = mock_repo_instance

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.repository_description == "Test repository"
        assert config.features.auto_address_review_enabled is True
        assert config.extend_exclude_patterns == ["tests/"]
        assert config.branch_name_convention == "always start with 'daiv/' followed by a short description."
        mock_cache.set.assert_called_once_with(
            f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}", config.model_dump(), CONFIGURATION_CACHE_TIMEOUT
        )

    @patch("daiv.core.config.cache")
    @patch("daiv.core.config.RepoClient")
    def test_get_config_with_default_values(self, mock_repo_client, mock_cache):
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_instance = Mock()
        mock_repo_instance.get_repository.return_value.default_branch = "main"
        mock_repo_instance.get_repository_file.return_value = None
        mock_repo_client.create_instance.return_value = mock_repo_instance

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.repository_description == ""
        assert config.features.auto_address_review_enabled is True
        assert config.extend_exclude_patterns == []
        assert config.branch_name_convention == "always start with 'daiv/' followed by a short description."
        mock_cache.set.assert_called_once_with(
            f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}", config.model_dump(), CONFIGURATION_CACHE_TIMEOUT
        )

    @patch("daiv.core.config.cache")
    def test_invalidate_cache(self, mock_cache):
        repo_id = "test_repo"
        RepositoryConfig.invalidate_cache(repo_id)
        mock_cache.delete.assert_called_once_with(f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}")

    @patch("daiv.core.config.cache")
    @patch("daiv.core.config.RepoClient")
    def test_get_config_with_invalid_yaml(self, mock_repo_client, mock_cache):
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_instance = Mock()
        mock_repo_instance.get_repository.return_value.default_branch = "main"
        mock_repo_instance.get_repository_file.return_value = "invalid_yaml: [unclosed_list"
        mock_repo_client.create_instance.return_value = mock_repo_instance

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.repository_description == ""
        assert config.features.auto_address_review_enabled is True
        assert config.extend_exclude_patterns == []
        assert config.branch_name_convention == "always start with 'daiv/' followed by a short description."
        mock_cache.set.assert_called_once_with(
            f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}", config.model_dump(), CONFIGURATION_CACHE_TIMEOUT
        )

    @patch("daiv.core.config.cache")
    @patch("daiv.core.config.RepoClient")
    def test_get_config_with_partial_yaml(self, mock_repo_client, mock_cache):
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_instance = Mock()
        mock_repo_instance.get_repository.return_value.default_branch = "main"
        mock_repo_instance.get_repository_file.return_value = """
        default_branch: main
        repository_description: Test repository
        """
        mock_repo_client.create_instance.return_value = mock_repo_instance

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.repository_description == "Test repository"
        assert config.features.auto_address_review_enabled is True
        assert config.extend_exclude_patterns == []
        assert config.branch_name_convention == "always start with 'daiv/' followed by a short description."
        mock_cache.set.assert_called_once_with(
            f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}", config.model_dump(), CONFIGURATION_CACHE_TIMEOUT
        )
