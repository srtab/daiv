from unittest.mock import Mock, patch

from django.core.management import call_command

import pytest
from gitlab import GitlabGetError

from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex


@pytest.fixture
def mock_repo_client():
    with patch.object(RepoClient, "create_instance") as mock:
        client = Mock(spec=RepoClient)
        mock.return_value = client
        yield client


@pytest.fixture
def mock_indexer():
    with patch("codebase.management.commands.update_index.CodebaseIndex") as mock:
        indexer = Mock(spec=CodebaseIndex)
        mock.return_value = indexer
        yield indexer


@pytest.fixture
def mock_thread_pool():
    with patch("codebase.management.commands.update_index.ThreadPoolExecutor") as mock:
        executor = Mock()
        mock.return_value.__enter__.return_value = executor
        yield executor


@pytest.mark.django_db  # this is needed to use transaction.atomic decorator
def test_update_index_all_repositories(mock_repo_client, mock_indexer, mock_thread_pool):
    """Test updating index for all repositories."""
    # Mock repositories
    mock_repo1 = Mock(slug="repo1")
    mock_repo2 = Mock(slug="repo2")
    mock_repo_client.list_repositories.return_value = [mock_repo1, mock_repo2]

    # Call the command
    call_command("update_index")

    # Verify repositories were listed with correct parameters
    mock_repo_client.list_repositories.assert_called_once_with(topics=None, load_all=True)

    # Verify ThreadPoolExecutor was used
    mock_thread_pool.map.assert_called_once()

    # Call the function directly for each repository to simulate the executor
    for repo in mock_thread_pool.map.call_args[0][1]:
        mock_thread_pool.map.call_args[0][0](repo)

    # Verify index was updated for each repository
    assert mock_indexer.update.call_count == 2
    mock_indexer.update.assert_any_call(repo_id="repo1", ref=None)
    mock_indexer.update.assert_any_call(repo_id="repo2", ref=None)


@pytest.mark.django_db  # this is needed to use transaction.atomic decorator
def test_update_index_specific_repository(mock_repo_client, mock_indexer, mock_thread_pool):
    """Test updating index for a specific repository."""
    # Mock repository
    mock_repo = Mock(slug="specific-repo")
    mock_repo_client.get_repository.return_value = mock_repo

    # Call the command
    call_command("update_index", repo_id="specific-repo")

    # Verify correct repository was fetched
    mock_repo_client.get_repository.assert_called_once_with("specific-repo")

    # Verify ThreadPoolExecutor was used
    mock_thread_pool.map.assert_called_once()

    # Call the function directly for each repository to simulate the executor
    for repo in mock_thread_pool.map.call_args[0][1]:
        mock_thread_pool.map.call_args[0][0](repo)

    # Verify index was updated only for the specific repository
    mock_indexer.update.assert_called_once_with(repo_id="specific-repo", ref=None)
    mock_repo_client.list_repositories.assert_not_called()


@pytest.mark.django_db  # this is needed to use transaction.atomic decorator
def test_update_index_with_topics(mock_repo_client, mock_indexer, mock_thread_pool):
    """Test updating index for repositories with specific topics."""
    # Mock repositories
    mock_repo1 = Mock(slug="repo1")
    mock_repo2 = Mock(slug="repo2")
    mock_repo_client.list_repositories.return_value = [mock_repo1, mock_repo2]

    # Call the command with topics
    call_command("update_index", topics=["python", "django"])

    # Verify repositories were listed with correct topics
    mock_repo_client.list_repositories.assert_called_once_with(topics=["python", "django"], load_all=True)

    # Verify ThreadPoolExecutor was used
    mock_thread_pool.map.assert_called_once()

    # Call the function directly for each repository to simulate the executor
    for repo in mock_thread_pool.map.call_args[0][1]:
        mock_thread_pool.map.call_args[0][0](repo)

    # Verify index was updated for each repository
    assert mock_indexer.update.call_count == 2
    mock_indexer.update.assert_any_call(repo_id="repo1", ref=None)
    mock_indexer.update.assert_any_call(repo_id="repo2", ref=None)


@pytest.mark.django_db  # this is needed to use transaction.atomic decorator
def test_update_index_with_ref(mock_repo_client, mock_indexer, mock_thread_pool):
    """Test updating index for a specific reference."""
    # Mock repository
    mock_repo = Mock(slug="repo1")
    mock_repo_client.list_repositories.return_value = [mock_repo]

    # Call the command with ref
    call_command("update_index", ref="feature-branch")

    # Verify ThreadPoolExecutor was used
    mock_thread_pool.map.assert_called_once()

    # Call the function directly for each repository to simulate the executor
    for repo in mock_thread_pool.map.call_args[0][1]:
        mock_thread_pool.map.call_args[0][0](repo)

    # Verify index was updated with correct reference
    mock_indexer.update.assert_called_once_with(repo_id="repo1", ref="feature-branch")


@pytest.mark.django_db  # this is needed to use transaction.atomic decorator
def test_update_index_with_reset(mock_repo_client, mock_indexer, mock_thread_pool):
    """Test updating index with reset option."""
    # Mock repository
    mock_repo = Mock(pk=1, slug="repo1")
    mock_repo_client.list_repositories.return_value = [mock_repo]

    # Call the command with reset
    call_command("update_index", reset=True)

    # Verify ThreadPoolExecutor was used
    mock_thread_pool.map.assert_called_once()

    # Call the function directly for each repository to simulate the executor
    for repo in mock_thread_pool.map.call_args[0][1]:
        mock_thread_pool.map.call_args[0][0](repo)

    # Verify index was reset and then updated
    mock_indexer.delete.assert_called_once_with(repo_id=1, ref=None, delete_all=False)
    mock_indexer.update.assert_called_once_with(repo_id="repo1", ref=None)


@pytest.mark.django_db  # this is needed to use transaction.atomic decorator
def test_update_index_with_reset_all(mock_repo_client, mock_indexer, mock_thread_pool):
    """Test updating index with reset all option."""
    # Mock repository
    mock_repo = Mock(pk=1, slug="repo1")
    mock_repo_client.list_repositories.return_value = [mock_repo]

    # Call the command with reset
    call_command("update_index", reset_all=True)

    # Verify ThreadPoolExecutor was used
    mock_thread_pool.map.assert_called_once()

    # Call the function directly for each repository to simulate the executor
    for repo in mock_thread_pool.map.call_args[0][1]:
        mock_thread_pool.map.call_args[0][0](repo)

    # Verify index was reset and then updated
    mock_indexer.delete.assert_called_once_with(repo_id=1, ref=None, delete_all=True)
    mock_indexer.update.assert_called_once_with(repo_id="repo1", ref=None)


@pytest.mark.django_db  # this is needed to use transaction.atomic decorator
def test_update_index_with_exclude_repo_ids(mock_repo_client, mock_indexer, mock_thread_pool):
    """Test updating index while excluding specific repositories."""
    # Mock repositories
    mock_repo1 = Mock(slug="repo1")
    mock_repo2 = Mock(slug="repo2")
    mock_repo3 = Mock(slug="repo3")
    mock_repo_client.list_repositories.return_value = [mock_repo1, mock_repo2, mock_repo3]

    # Call the command with exclude_repo_ids
    call_command("update_index", exclude_repo_ids=["repo2"])

    # Verify repositories were listed with correct parameters
    mock_repo_client.list_repositories.assert_called_once_with(topics=None, load_all=True)

    # Verify ThreadPoolExecutor was used
    mock_thread_pool.map.assert_called_once()

    # Call the function directly for each repository to simulate the executor
    for repo in mock_thread_pool.map.call_args[0][1]:
        mock_thread_pool.map.call_args[0][0](repo)

    # Verify index was updated only for non-excluded repositories
    assert mock_indexer.update.call_count == 2
    mock_indexer.update.assert_any_call(repo_id="repo1", ref=None)
    mock_indexer.update.assert_any_call(repo_id="repo3", ref=None)

    # Verify excluded repository was not updated
    for call_args in mock_indexer.update.call_args_list:
        args, kwargs = call_args
        assert kwargs.get("repo_id") != "repo2"


def test_update_index_repository_not_found(mock_repo_client, mock_indexer):
    """Test handling of non-existent repository."""
    # Mock GitlabGetError for non-existent repository
    mock_repo_client.get_repository.side_effect = GitlabGetError()

    # Call the command with non-existent repository
    call_command("update_index", repo_id="non-existent-repo")

    # Verify error was handled gracefully
    mock_repo_client.get_repository.assert_called_once_with("non-existent-repo")
    mock_indexer.update.assert_not_called()
    mock_indexer.delete.assert_not_called()


def test_update_index_no_repositories(mock_repo_client, mock_indexer):
    """Test behavior when no repositories are found."""
    # Mock empty repository list
    mock_repo_client.list_repositories.return_value = []

    # Call the command
    call_command("update_index")

    # Verify no updates were performed
    mock_indexer.update.assert_not_called()
    mock_indexer.delete.assert_not_called()
