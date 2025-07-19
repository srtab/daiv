"""
Tests for the cleanup functionality.
"""

from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError

import pytest
from gitlab import GitlabGetError

from codebase.indexes import CodebaseIndex
from codebase.models import CodebaseNamespace, RepositoryInfo


@pytest.fixture
def mock_indexer():
    with patch("codebase.management.commands.cleanup_indexes.CodebaseIndex") as mock:
        indexer = Mock(spec=CodebaseIndex)
        mock.return_value = indexer
        yield indexer


@pytest.fixture
def repo_info():
    return RepositoryInfo.objects.create(
        external_slug="test-org/test-repo", external_id="123", client="gitlab", default_branch="main"
    )


@pytest.fixture
def old_namespace(repo_info):
    from django.utils import timezone

    old_namespace_obj = CodebaseNamespace.objects.create(
        repository_info=repo_info, sha="def456", tracking_ref="feature-branch", status=CodebaseNamespace.Status.INDEXED
    )
    # on creation, the created and modified fields are set to the current time
    old_namespace_obj.created = timezone.now() - timedelta(days=45)
    old_namespace_obj.save()

    return old_namespace_obj


@pytest.fixture
def default_namespace(repo_info):
    return CodebaseNamespace.objects.create(
        repository_info=repo_info, sha="abc123", tracking_ref="main", status=CodebaseNamespace.Status.INDEXED
    )


@pytest.fixture
def failed_namespace(repo_info):
    return CodebaseNamespace.objects.create(
        repository_info=repo_info, sha="ghi789", tracking_ref="failed-branch", status=CodebaseNamespace.Status.FAILED
    )


@pytest.mark.django_db
def test_cleanup_command_requires_operation():
    """Test that the command requires at least one cleanup operation to be specified."""
    # Should fail when no operations are specified
    with pytest.raises(CommandError):
        call_command("cleanup_indexes")


@pytest.mark.django_db
def test_cleanup_inaccessible_repositories(mock_repo_client, repo_info):
    """Test cleanup of inaccessible repositories."""
    # Mock GitLab error (repository not found)
    mock_repo_client.get_repository.side_effect = GitlabGetError(response_code=404, error_message="Not found")

    # Call the command with no-input to avoid interactive prompt
    call_command("cleanup_indexes", check_accessibility=True, no_input=True)

    # Verify repo client was called
    mock_repo_client.get_repository.assert_called_once_with("test-org/test-repo")

    # Verify the repository info was deleted
    assert not RepositoryInfo.objects.filter(id=repo_info.id).exists()


@pytest.mark.django_db
def test_cleanup_inaccessible_repositories_dry_run(mock_repo_client, mock_indexer, repo_info):
    """Test dry run for cleanup of inaccessible repositories."""
    # Mock GitLab error (repository not found)
    mock_repo_client.get_repository.side_effect = GitlabGetError(response_code=404, error_message="Not found")

    # Call the command with dry-run
    call_command("cleanup_indexes", check_accessibility=True, dry_run=True)

    # Verify repo client was called
    mock_repo_client.get_repository.assert_called_once_with("test-org/test-repo")

    # Verify indexer delete was NOT called due to dry run
    mock_indexer.delete.assert_not_called()


@pytest.mark.django_db
def test_cleanup_skips_temporary_errors(mock_repo_client, mock_indexer, repo_info):
    """Test that temporary errors don't trigger cleanup."""
    # Mock temporary error (server error)
    mock_repo_client.get_repository.side_effect = GitlabGetError(response_code=500, error_message="Server error")

    # Call the command
    call_command("cleanup_indexes", check_accessibility=True, no_input=True)

    # Verify repo client was called
    mock_repo_client.get_repository.assert_called_once_with("test-org/test-repo")

    # Verify indexer delete was NOT called due to temporary error
    mock_indexer.delete.assert_not_called()


@pytest.mark.django_db
def test_cleanup_skips_accessible_repositories(mock_repo_client, mock_indexer, repo_info):
    """Test that accessible repositories are not cleaned up."""
    # Mock successful repository access
    mock_repo = Mock()
    mock_repo_client.get_repository.return_value = mock_repo

    # Call the command
    call_command("cleanup_indexes", check_accessibility=True, no_input=True)

    # Verify repo client was called
    mock_repo_client.get_repository.assert_called_once_with("test-org/test-repo")

    # Verify indexer delete was NOT called for accessible repo
    mock_indexer.delete.assert_not_called()


@pytest.mark.django_db
def test_cleanup_old_branch_indexes(old_namespace, default_namespace):
    """Test cleanup of old branch indexes."""
    # Call the command
    call_command("cleanup_indexes", cleanup_old_branches=True, branch_age_days=30, no_input=True)

    # Verify the old namespace was deleted
    assert not CodebaseNamespace.objects.filter(id=old_namespace.id).exists()

    # Verify the default branch namespace still exists
    assert CodebaseNamespace.objects.filter(id=default_namespace.id).exists()


@pytest.mark.django_db
def test_cleanup_old_branch_indexes_dry_run(mock_indexer, old_namespace, default_namespace):
    """Test dry run for cleanup of old branch indexes."""
    # Call the command with dry-run
    call_command("cleanup_indexes", cleanup_old_branches=True, branch_age_days=30, dry_run=True)

    # Verify indexer delete was NOT called due to dry run
    mock_indexer.delete.assert_not_called()

    # Verify both namespaces still exist
    assert CodebaseNamespace.objects.filter(id=old_namespace.id).exists()
    assert CodebaseNamespace.objects.filter(id=default_namespace.id).exists()


@pytest.mark.django_db
def test_cleanup_failed_indexes(failed_namespace, default_namespace):
    """Test cleanup of failed indexes."""
    # Call the command
    call_command("cleanup_indexes", cleanup_old_branches=True, no_input=True)

    # Verify the failed namespace was deleted
    assert not CodebaseNamespace.objects.filter(id=failed_namespace.id).exists()

    # Verify the default branch namespace still exists
    assert CodebaseNamespace.objects.filter(id=default_namespace.id).exists()


@pytest.mark.django_db
def test_cleanup_preserves_default_branches(mock_indexer, default_namespace):
    """Test that default branch indexes are never cleaned up."""
    from django.utils import timezone

    # Make the default namespace old too
    old_date = timezone.now() - timedelta(days=45)
    default_namespace.created = old_date
    default_namespace.save()

    # Call the command
    call_command("cleanup_indexes", cleanup_old_branches=True, branch_age_days=30, no_input=True)

    # Verify indexer delete was NOT called
    mock_indexer.delete.assert_not_called()

    # Verify the default branch namespace still exists
    assert CodebaseNamespace.objects.filter(id=default_namespace.id).exists()


@pytest.mark.django_db
def test_cleanup_with_specific_repo_id(mock_repo_client, repo_info):
    """Test cleanup limited to a specific repository."""
    # Create another repository
    other_repo = RepositoryInfo.objects.create(external_slug="other-org/other-repo", external_id="456", client="gitlab")

    # Mock GitLab error for specific repo only
    def mock_get_repo(slug):
        if slug == "test-org/test-repo":
            raise GitlabGetError(response_code=404, error_message="Not found")
        return Mock()

    mock_repo_client.get_repository.side_effect = mock_get_repo

    # Call the command with specific repo_id
    call_command("cleanup_indexes", check_accessibility=True, repo_id="test-org/test-repo", no_input=True)

    # Verify only the specified repo was checked
    mock_repo_client.get_repository.assert_called_once_with("test-org/test-repo")

    # Verify the specific repository info was deleted
    assert not RepositoryInfo.objects.filter(id=repo_info.id).exists()

    # Verify the other repository still exists
    assert RepositoryInfo.objects.filter(id=other_repo.id).exists()


@pytest.mark.django_db
def test_cleanup_all_operations(mock_repo_client, repo_info, old_namespace):
    """Test running all cleanup operations with --all flag."""
    # Mock GitLab error for accessibility check
    mock_repo_client.get_repository.side_effect = GitlabGetError(response_code=404, error_message="Not found")

    # Call the command with --all flag
    call_command("cleanup_indexes", all=True, no_input=True)

    # Verify repo client was called for accessibility check
    mock_repo_client.get_repository.assert_called_with("test-org/test-repo")

    # Verify both the repository and old namespace were deleted
    assert not RepositoryInfo.objects.filter(id=repo_info.id).exists()
    assert not CodebaseNamespace.objects.filter(id=old_namespace.id).exists()


@pytest.mark.django_db
def test_cleanup_no_old_branches_found(mock_indexer, default_namespace):
    """Test behavior when no old branch indexes are found."""
    # Call the command
    call_command("cleanup_indexes", cleanup_old_branches=True, no_input=True)

    # Verify indexer delete was NOT called
    mock_indexer.delete.assert_not_called()


@pytest.mark.django_db
def test_cleanup_no_inaccessible_repos_found(mock_repo_client, mock_indexer, repo_info):
    """Test behavior when no inaccessible repositories are found."""
    # Mock successful repository access
    mock_repo = Mock()
    mock_repo_client.get_repository.return_value = mock_repo

    # Call the command
    call_command("cleanup_indexes", check_accessibility=True, no_input=True)

    # Verify repo client was called
    mock_repo_client.get_repository.assert_called_once_with("test-org/test-repo")

    # Verify indexer delete was NOT called
    mock_indexer.delete.assert_not_called()


@pytest.mark.django_db
def test_cleanup_with_custom_branch_age_days(repo_info):
    """Test cleanup with custom branch age threshold."""
    from django.utils import timezone

    # Create a namespace that's 15 days old (should not be deleted with 30-day threshold)
    recent_date = timezone.now() - timedelta(days=15)
    recent_namespace = CodebaseNamespace.objects.create(
        repository_info=repo_info,
        sha="recent123",
        tracking_ref="recent-branch",
        status=CodebaseNamespace.Status.INDEXED,
    )
    recent_namespace.created = recent_date
    recent_namespace.save()

    # Call the command with 10-day threshold (should delete the 15-day old namespace)
    call_command("cleanup_indexes", cleanup_old_branches=True, branch_age_days=10, no_input=True)

    # Verify the recent namespace was deleted
    assert not CodebaseNamespace.objects.filter(id=recent_namespace.id).exists()


@pytest.mark.django_db
@patch("builtins.input", return_value="n")
def test_cleanup_user_cancels_operation(mock_input, mock_repo_client, mock_indexer, repo_info):
    """Test that cleanup is cancelled when user responds 'n' to confirmation."""
    # Mock GitLab error
    mock_repo_client.get_repository.side_effect = GitlabGetError(response_code=404, error_message="Not found")

    # Call the command without no_input (should prompt user)
    call_command("cleanup_indexes", check_accessibility=True)

    # Verify user was prompted
    mock_input.assert_called_once_with("Do you want to proceed? [y/N]: ")

    # Verify indexer delete was NOT called due to user cancellation
    mock_indexer.delete.assert_not_called()


@pytest.mark.django_db
@patch("builtins.input", return_value="y")
def test_cleanup_user_confirms_operation(mock_input, mock_repo_client, repo_info):
    """Test that cleanup proceeds when user responds 'y' to confirmation."""
    # Mock GitLab error
    mock_repo_client.get_repository.side_effect = GitlabGetError(response_code=404, error_message="Not found")

    # Call the command without no_input (should prompt user)
    call_command("cleanup_indexes", check_accessibility=True)

    # Verify user was prompted
    mock_input.assert_called_once_with("Do you want to proceed? [y/N]: ")

    # Verify repository was deleted
    assert not RepositoryInfo.objects.filter(id=repo_info.id).exists()


@pytest.mark.django_db
def test_cleanup_keyboard_interrupt(mock_repo_client, mock_indexer, repo_info):
    """Test that cleanup is cancelled gracefully on KeyboardInterrupt."""
    # Mock GitLab error
    mock_repo_client.get_repository.side_effect = GitlabGetError(response_code=404, error_message="Not found")

    # Call the command without no_input (should prompt user)
    with patch("builtins.input", side_effect=KeyboardInterrupt):
        call_command("cleanup_indexes", check_accessibility=True)

    # Verify indexer delete was NOT called due to KeyboardInterrupt
    mock_indexer.delete.assert_not_called()
