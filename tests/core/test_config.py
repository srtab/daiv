from unittest.mock import Mock, patch

from core.config import (
    BRANCH_NAME_CONVENTION_MAX_LENGTH,
    CONFIGURATION_CACHE_KEY_PREFIX,
    CONFIGURATION_CACHE_TIMEOUT,
    REPOSITORY_DESCRIPTION_MAX_LENGTH,
    RepositoryConfig,
)


class RepositoryConfigTest:
    @patch("core.config.cache")
    def test_get_config_from_cache(self, mock_cache):
        repo_id = "test_repo"
        cached_config = {
            "default_branch": "main",
            "repository_description": "Test repository",
            "features": {"auto_address_review_enabled": True, "auto_address_issues_enabled": True},
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

    @patch("core.config.cache")
    def test_get_config_from_repo(self, mock_cache, mock_repo_client):
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_client.get_repository.return_value.default_branch = "main"
        mock_repo_client.get_repository_file.return_value = """
        default_branch: main
        repository_description: Test repository
        features:
          auto_address_review_enabled: true
          auto_address_issues_enabled: true
        extend_exclude_patterns:
          - tests/
        branch_name_convention: always start with 'daiv/' followed by a short description.
        """

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.repository_description == "Test repository"
        assert config.features.auto_address_review_enabled is True
        assert config.extend_exclude_patterns == ["tests/"]
        assert config.branch_name_convention == "always start with 'daiv/' followed by a short description."
        mock_cache.set.assert_called_once_with(
            f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}", config.model_dump(), CONFIGURATION_CACHE_TIMEOUT
        )

    @patch("core.config.cache")
    def test_get_config_with_default_values(self, mock_cache, mock_repo_client):
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_client.get_repository.return_value.default_branch = "main"
        mock_repo_client.get_repository_file.return_value = None

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.repository_description == ""
        assert config.features.auto_address_review_enabled is True
        assert config.extend_exclude_patterns == []
        assert config.branch_name_convention == "always start with 'daiv/' followed by a short description."
        mock_cache.set.assert_called_once_with(
            f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}", config.model_dump(), CONFIGURATION_CACHE_TIMEOUT
        )

    @patch("core.config.cache")
    def test_invalidate_cache(self, mock_cache):
        repo_id = "test_repo"
        RepositoryConfig.invalidate_cache(repo_id)
        mock_cache.delete.assert_called_once_with(f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}")

    @patch("core.config.cache")
    def test_get_config_with_invalid_yaml(self, mock_cache, mock_repo_client):
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_client.get_repository.return_value.default_branch = "main"
        mock_repo_client.get_repository_file.return_value = "invalid_yaml: [unclosed_list"

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.repository_description == ""
        assert config.features.auto_address_review_enabled is True
        assert config.extend_exclude_patterns == []
        assert config.branch_name_convention == "always start with 'daiv/' followed by a short description."
        mock_cache.set.assert_called_once_with(
            f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}", config.model_dump(), CONFIGURATION_CACHE_TIMEOUT
        )

    @patch("core.config.cache")
    def test_get_config_with_partial_yaml(self, mock_cache, mock_repo_client):
        repo_id = "test_repo"
        mock_cache.get.return_value = None
        mock_repo_client.get_repository.return_value.default_branch = "main"
        mock_repo_client.get_repository_file.return_value = """
        default_branch: main
        repository_description: Test repository
        """

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.repository_description == "Test repository"
        assert config.features.auto_address_review_enabled is True
        assert config.extend_exclude_patterns == []
        assert config.branch_name_convention == "always start with 'daiv/' followed by a short description."
        mock_cache.set.assert_called_once_with(
            f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}", config.model_dump(), CONFIGURATION_CACHE_TIMEOUT
        )

    def test_truncate_if_too_long(self):
        long_description = "a" * (REPOSITORY_DESCRIPTION_MAX_LENGTH + 10)
        truncated_description = RepositoryConfig.truncate_if_too_long(
            long_description, info=Mock(field_name="repository_description")
        )
        assert len(truncated_description) == REPOSITORY_DESCRIPTION_MAX_LENGTH

        long_branch_name = "b" * (BRANCH_NAME_CONVENTION_MAX_LENGTH + 10)
        truncated_branch_name = RepositoryConfig.truncate_if_too_long(
            long_branch_name, info=Mock(field_name="branch_name_convention")
        )
        assert len(truncated_branch_name) == BRANCH_NAME_CONVENTION_MAX_LENGTH

    def test_combined_exclude_patterns(self):
        config = RepositoryConfig(extend_exclude_patterns=["**/custom_pattern/**"])
        combined_patterns = config.combined_exclude_patterns
        assert "**/custom_pattern/**" in combined_patterns
        assert "**/.git/**" in combined_patterns
