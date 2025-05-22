from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.utils import timezone

import pytest
from gitlab import GitlabGetError

from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex
from codebase.models import CodebaseDocument, CodebaseNamespace, RepositoryInfo


@pytest.fixture
def mock_repo_client():
    with patch.object(RepoClient, "create_instance") as mock:
        client = Mock(spec=RepoClient)
        mock.return_value = client
        yield client


@pytest.fixture
def mock_indexer():
    with patch("codebase.management.commands.cleanup_indexes.CodebaseIndex") as mock:
        indexer = Mock(spec=CodebaseIndex)
        mock.return_value = indexer
        yield indexer


@pytest.fixture
def sample_repository_info():
    """Create a sample RepositoryInfo for testing."""
    return RepositoryInfo.objects.create(external_slug="test/repo", external_id="123", client="gitlab")


@pytest.fixture
def sample_namespace(sample_repository_info):
    """Create a sample CodebaseNamespace for testing."""
    return CodebaseNamespace.objects.create(
        repository_info=sample_repository_info,
        sha="abc123",
        tracking_ref="main",
        status=CodebaseNamespace.Status.INDEXED,
    )


@pytest.fixture
def old_namespace(sample_repository_info):
    """Create an old CodebaseNamespace for testing."""
    old_date = timezone.now() - timedelta(days=35)
    namespace = CodebaseNamespace.objects.create(
        repository_info=sample_repository_info,
        sha="def456",
        tracking_ref="feature-branch",
        status=CodebaseNamespace.Status.INDEXED,
    )
    # Manually set the created date to be old
    namespace.created = old_date
    namespace.save()
    return namespace


@pytest.mark.django_db
def test_cleanup_inaccessible_repositories(mock_repo_client, mock_indexer, sample_repository_info, sample_namespace):
    """Test cleanup of repositories that raise GitlabGetError."""
    # Mock repository access to raise GitlabGetError
    mock_repo_client.get_repository.side_effect = GitlabGetError()

    # Create a document for the namespace
    CodebaseDocument.objects.create(
        namespace=sample_namespace,
        source="test.py",
        page_content="test content",
        page_content_vector=[0.1, 0.2, 0.3],
        is_default_branch=True,
    )

    # Call the command
    call_command("cleanup_indexes")

    # Verify repository access was checked
    mock_repo_client.get_repository.assert_called_once_with("test/repo")

    # Verify indexer.delete was called for the inaccessible repository
    mock_indexer.delete.assert_called_once_with(repo_id="test/repo", ref="main")


@pytest.mark.django_db
def test_accessible_repositories_not_deleted(mock_repo_client, mock_indexer, sample_repository_info, sample_namespace):
    """Test that accessible repositories are not deleted."""
    # Mock repository access to succeed
    mock_repo_client.get_repository.return_value = Mock()

    # Call the command
    call_command("cleanup_indexes")

    # Verify repository access was checked
    mock_repo_client.get_repository.assert_called_once_with("test/repo")

    # Verify indexer.delete was not called for accessible repository
    mock_indexer.delete.assert_not_called()


@pytest.mark.django_db
def test_cleanup_old_non_default_branch_namespaces(mock_repo_client, mock_indexer, old_namespace):
    """Test cleanup of old non-default branch namespaces."""
    # Mock repository access to succeed
    mock_repo_client.get_repository.return_value = Mock()

    # Create a non-default branch document
    CodebaseDocument.objects.create(
        namespace=old_namespace,
        source="test.py",
        page_content="test content",
        page_content_vector=[0.1, 0.2, 0.3],
        is_default_branch=False,
    )

    # Call the command with default age threshold (30 days)
    call_command("cleanup_indexes")

    # Verify indexer.delete was called for the old namespace
    mock_indexer.delete.assert_called_with(repo_id="test/repo", ref="feature-branch")


@pytest.mark.django_db
def test_recent_non_default_branch_namespaces_preserved(mock_repo_client, mock_indexer, sample_repository_info):
    """Test that recent non-default branch namespaces are preserved."""
    # Mock repository access to succeed
    mock_repo_client.get_repository.return_value = Mock()

    # Create a recent namespace (within 30 days)
    recent_namespace = CodebaseNamespace.objects.create(
        repository_info=sample_repository_info,
        sha="recent123",
        tracking_ref="recent-feature",
        status=CodebaseNamespace.Status.INDEXED,
    )

    # Create a non-default branch document
    CodebaseDocument.objects.create(
        namespace=recent_namespace,
        source="test.py",
        page_content="test content",
        page_content_vector=[0.1, 0.2, 0.3],
        is_default_branch=False,
    )

    # Call the command
    call_command("cleanup_indexes")

    # Verify indexer.delete was not called for recent namespace
    mock_indexer.delete.assert_not_called()


@pytest.mark.django_db
def test_custom_age_threshold(mock_repo_client, mock_indexer, sample_repository_info):
    """Test custom age threshold parameter handling."""
    # Mock repository access to succeed
    mock_repo_client.get_repository.return_value = Mock()

    # Create a namespace that's 20 days old
    old_date = timezone.now() - timedelta(days=20)
    namespace = CodebaseNamespace.objects.create(
        repository_info=sample_repository_info,
        sha="test123",
        tracking_ref="test-branch",
        status=CodebaseNamespace.Status.INDEXED,
    )
    namespace.created = old_date
    namespace.save()

    # Create a non-default branch document
    CodebaseDocument.objects.create(
        namespace=namespace,
        source="test.py",
        page_content="test content",
        page_content_vector=[0.1, 0.2, 0.3],
        is_default_branch=False,
    )

    # Call the command with 15-day threshold
    call_command("cleanup_indexes", branch_age_days=15)

    # Verify indexer.delete was called (20 days > 15 days)
    mock_indexer.delete.assert_called_once_with(repo_id="test/repo", ref="test-branch")


@pytest.mark.django_db
def test_dry_run_functionality(mock_repo_client, mock_indexer, sample_repository_info, old_namespace):
    """Test dry-run functionality shows correct preview without making changes."""
    # Mock repository access to raise GitlabGetError
    mock_repo_client.get_repository.side_effect = GitlabGetError()

    # Create a non-default branch document for old namespace
    CodebaseDocument.objects.create(
        namespace=old_namespace,
        source="test.py",
        page_content="test content",
        page_content_vector=[0.1, 0.2, 0.3],
        is_default_branch=False,
    )

    # Call the command with dry-run
    call_command("cleanup_indexes", dry_run=True)

    # Verify repository access was checked
    mock_repo_client.get_repository.assert_called_once_with("test/repo")

    # Verify indexer.delete was not called in dry-run mode
    mock_indexer.delete.assert_not_called()


@pytest.mark.django_db
def test_default_branch_namespaces_not_affected(mock_repo_client, mock_indexer, sample_repository_info):
    """Test that default branch namespaces are not affected by age cleanup."""
    # Mock repository access to succeed
    mock_repo_client.get_repository.return_value = Mock()

    # Create an old namespace with default branch documents
    old_date = timezone.now() - timedelta(days=35)
    namespace = CodebaseNamespace.objects.create(
        repository_info=sample_repository_info,
        sha="old123",
        tracking_ref="main",
        status=CodebaseNamespace.Status.INDEXED,
    )
    namespace.created = old_date
    namespace.save()

    # Create a default branch document
    CodebaseDocument.objects.create(
        namespace=namespace,
        source="test.py",
        page_content="test content",
        page_content_vector=[0.1, 0.2, 0.3],
        is_default_branch=True,
    )

    # Call the command
    call_command("cleanup_indexes")

    # Verify indexer.delete was not called for default branch namespace
    mock_indexer.delete.assert_not_called()


@pytest.mark.django_db
def test_error_handling_during_cleanup(mock_repo_client, mock_indexer, sample_repository_info, sample_namespace):
    """Test error handling when cleanup fails."""
    # Mock repository access to raise GitlabGetError
    mock_repo_client.get_repository.side_effect = GitlabGetError()

    # Mock indexer.delete to raise an exception
    mock_indexer.delete.side_effect = Exception("Cleanup failed")

    # Call the command and expect it to raise the exception
    with pytest.raises(Exception, match="Cleanup failed"):
        call_command("cleanup_indexes")


@pytest.mark.django_db
def test_no_repositories_to_cleanup(mock_repo_client, mock_indexer):
    """Test behavior when no repositories need cleanup."""
    # Mock repository access to succeed (no repositories in database)
    mock_repo_client.get_repository.return_value = Mock()

    # Call the command
    call_command("cleanup_indexes")

    # Verify no repository access was attempted (no repositories exist)
    mock_repo_client.get_repository.assert_not_called()

    # Verify indexer.delete was not called
    mock_indexer.delete.assert_not_called()


@pytest.mark.django_db
def test_multiple_repositories_mixed_accessibility(mock_repo_client, mock_indexer):
    """Test handling of multiple repositories with mixed accessibility."""
    # Create multiple repositories
    repo1 = RepositoryInfo.objects.create(external_slug="test/repo1", external_id="123", client="gitlab")
    repo2 = RepositoryInfo.objects.create(external_slug="test/repo2", external_id="456", client="gitlab")

    # Create namespaces for both repositories
    CodebaseNamespace.objects.create(
        repository_info=repo1, sha="abc123", tracking_ref="main", status=CodebaseNamespace.Status.INDEXED
    )
    CodebaseNamespace.objects.create(
        repository_info=repo2, sha="def456", tracking_ref="main", status=CodebaseNamespace.Status.INDEXED
    )

    # Mock repository access: repo1 accessible, repo2 inaccessible
    def mock_get_repository(repo_slug):
        if repo_slug == "test/repo1":
            return Mock()
        elif repo_slug == "test/repo2":
            raise GitlabGetError()

    mock_repo_client.get_repository.side_effect = mock_get_repository

    # Call the command
    call_command("cleanup_indexes")

    # Verify both repositories were checked
    assert mock_repo_client.get_repository.call_count == 2

    # Verify only the inaccessible repository was cleaned up
    mock_indexer.delete.assert_called_once_with(repo_id="test/repo2", ref="main")


@pytest.mark.django_db
def test_namespace_with_mixed_document_types(mock_repo_client, mock_indexer, sample_repository_info):
    """Test namespace cleanup with both default and non-default branch documents."""
    # Mock repository access to succeed
    mock_repo_client.get_repository.return_value = Mock()

    # Create an old namespace
    old_date = timezone.now() - timedelta(days=35)
    namespace = CodebaseNamespace.objects.create(
        repository_info=sample_repository_info,
        sha="mixed123",
        tracking_ref="mixed-branch",
        status=CodebaseNamespace.Status.INDEXED,
    )
    namespace.created = old_date
    namespace.save()

    # Create both default and non-default branch documents
    CodebaseDocument.objects.create(
        namespace=namespace,
        source="default.py",
        page_content="default content",
        page_content_vector=[0.1, 0.2, 0.3],
        is_default_branch=True,
    )
    CodebaseDocument.objects.create(
        namespace=namespace,
        source="feature.py",
        page_content="feature content",
        page_content_vector=[0.4, 0.5, 0.6],
        is_default_branch=False,
    )

    # Call the command
    call_command("cleanup_indexes")

    # Verify the namespace was cleaned up (has non-default branch documents)
    mock_indexer.delete.assert_called_once_with(repo_id="test/repo", ref="mixed-branch")
