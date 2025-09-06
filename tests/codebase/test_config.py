from unittest.mock import Mock, patch

from codebase.repo_config import (
    BRANCH_NAME_CONVENTION_MAX_LENGTH,
    CONFIGURATION_CACHE_KEY_PREFIX,
    CONFIGURATION_CACHE_TIMEOUT,
    PullRequest,
    RepositoryConfig,
)


class RepositoryConfigTest:
    @patch("codebase.repo_config.cache")
    def test_get_config_from_cache(self, mock_cache):
        repo_id = "test_repo"
        cached_config = {
            "default_branch": "main",
            "code_review": {"enabled": True},
            "issue_addressing": {"enabled": True},
            "quick_actions": {"enabled": True},
            "pull_request": {"branch_name_convention": "always start with 'daiv/' followed by a short description."},
            "extend_exclude_patterns": ["tests/"],
        }
        mock_cache.get.return_value = cached_config

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.code_review.enabled is True
        assert config.issue_addressing.enabled is True
        assert config.quick_actions.enabled is True
        assert (
            config.pull_request.branch_name_convention == "always start with 'daiv/' followed by a short description."
        )
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
        pull_request:
          branch_name_convention: always start with 'daiv/' followed by a short description.
        extend_exclude_patterns:
          - tests/
        """

        config = RepositoryConfig.get_config(repo_id)

        assert config.default_branch == "main"
        assert config.code_review.enabled is True
        assert config.issue_addressing.enabled is True
        assert config.quick_actions.enabled is True
        assert (
            config.pull_request.branch_name_convention == "always start with 'daiv/' followed by a short description."
        )
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
        assert (
            config.pull_request.branch_name_convention == "always start with 'daiv/' followed by a short description."
        )
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
        assert (
            config.pull_request.branch_name_convention == "always start with 'daiv/' followed by a short description."
        )
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
        assert (
            config.pull_request.branch_name_convention == "always start with 'daiv/' followed by a short description."
        )
        assert config.extend_exclude_patterns == []
        mock_cache.set.assert_called_once_with(
            f"{CONFIGURATION_CACHE_KEY_PREFIX}{repo_id}", config.model_dump(), CONFIGURATION_CACHE_TIMEOUT
        )

    def test_pull_request_truncate_if_too_long(self):
        long_branch_name = "b" * (BRANCH_NAME_CONVENTION_MAX_LENGTH + 10)
        truncated_branch_name = PullRequest.truncate_if_too_long(
            long_branch_name, info=Mock(field_name="branch_name_convention")
        )
        assert len(truncated_branch_name) == BRANCH_NAME_CONVENTION_MAX_LENGTH

    def test_combined_exclude_patterns(self):
        config = RepositoryConfig(extend_exclude_patterns=["**/custom_pattern/**"])
        combined_patterns = config.combined_exclude_patterns
        assert "**/custom_pattern/**" in combined_patterns
        assert "**/.git/**" in combined_patterns
