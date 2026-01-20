from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo

from codebase.utils import GitManager


def _configure_repo_identity(repo: Repo) -> None:
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test User")
        writer.set_value("user", "email", "test@example.com")


def _init_repo(tmp_path: Path) -> Repo:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = Repo.init(repo_dir)
    _configure_repo_identity(repo)
    return repo


def _repo_path(repo: Repo) -> Path:
    if repo.working_tree_dir is None:
        raise RuntimeError("Repository working tree was not available.")
    return Path(repo.working_tree_dir)


def _create_initial_commit(repo: Repo, repo_dir: Path) -> None:
    (repo_dir / "README.md").write_text("initial\n")
    repo.git.add("-A")
    repo.index.commit("Initial commit")
    repo.git.branch("-M", "main")
    repo.remotes.origin.push("main")


def _init_repo_with_origin(tmp_path: Path) -> tuple[Repo, Path]:
    origin_dir = tmp_path / "origin.git"
    Repo.init(origin_dir, bare=True)
    repo_dir = tmp_path / "work"
    repo_dir.mkdir()
    repo = Repo.init(repo_dir)
    _configure_repo_identity(repo)
    repo.create_remote("origin", origin_dir.as_posix())
    _create_initial_commit(repo, repo_dir)
    return repo, origin_dir


def test_git_manager_is_dirty_detects_new_untracked_files(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = Repo.init(repo_dir)

    # Prime GitPython's internal state (this is where caching can bite).
    _ = repo.untracked_files

    (repo_dir / "new_file.txt").write_text("hello\n")

    assert GitManager(repo).is_dirty() is True


def test_git_manager_is_dirty_returns_false_for_clean_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    repo_dir = _repo_path(repo)
    (repo_dir / "README.md").write_text("hello\n")
    repo.git.add("-A")
    repo.index.commit("Initial commit")

    assert GitManager(repo).is_dirty() is False


def test_git_manager_get_untracked_files_detects_new_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    repo_dir = _repo_path(repo)

    _ = repo.untracked_files
    (repo_dir / "new_file.txt").write_text("hello\n")

    assert "new_file.txt" in GitManager(repo)._get_untracked_files()


def test_git_manager_get_diff_includes_untracked_file_diff(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = Repo.init(repo_dir)

    (repo_dir / "new_file.txt").write_text("hello\n")

    diff = GitManager(repo).get_diff()
    # For untracked files we add `git diff --no-index /dev/null <file>`, which includes `/dev/null`.
    assert "new_file.txt" in diff


def test_git_manager_commit_and_push_creates_branch_and_pushes(tmp_path: Path) -> None:
    repo, origin_dir = _init_repo_with_origin(tmp_path)
    repo_dir = _repo_path(repo)
    (repo_dir / "feature.txt").write_text("feature\n")

    branch_name = GitManager(repo).commit_and_push_changes("Add feature", branch_name="feature/test")

    assert branch_name == "feature/test"
    assert repo.active_branch.name == "feature/test"
    assert repo.head.commit.message.strip() == "Add feature"

    origin_repo = Repo(origin_dir)
    assert branch_name in [head.name for head in origin_repo.heads]


def test_git_manager_commit_and_push_adds_skip_ci_prefix(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)
    repo_dir = _repo_path(repo)
    (repo_dir / "skip.txt").write_text("skip\n")

    GitManager(repo).commit_and_push_changes("Add skip", branch_name="skip-ci", skip_ci=True)

    assert repo.head.commit.message.strip() == "[skip ci] Add skip"


def test_git_manager_commit_and_push_generates_unique_branch_name(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)
    repo.git.branch("feature")
    repo_dir = _repo_path(repo)
    (repo_dir / "unique.txt").write_text("unique\n")

    branch_name = GitManager(repo).commit_and_push_changes(
        "Add unique", branch_name="feature", use_branch_if_exists=False
    )

    assert branch_name == "feature-1"
    assert repo.active_branch.name == "feature-1"


def test_git_manager_checkout_raises_for_missing_branch(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)

    with pytest.raises(ValueError, match="Branch missing-branch does not exist in the repository."):
        GitManager(repo).checkout("missing-branch")


def test_git_manager_gen_unique_branch_name_returns_original_when_available(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    manager = GitManager(repo)

    assert manager._gen_unique_branch_name("feature", ["main"]) == "feature"


def test_git_manager_gen_unique_branch_name_raises_when_max_attempts_exceeded(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    manager = GitManager(repo)

    with pytest.raises(ValueError, match="max attempts reached 3"):
        manager._gen_unique_branch_name("feature", ["feature", "feature-1", "feature-2"], max_attempts=3)


def test_git_manager_apply_patch_applies_valid_diff(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    repo_dir = _repo_path(repo)
    file_path = repo_dir / "file.txt"
    file_path.write_text("hello\n")
    repo.git.add("-A")
    repo.index.commit("Initial commit")

    file_path.write_text("hello\nworld\n")
    diff = repo.git.diff("HEAD")
    file_path.write_text("hello\n")

    GitManager(repo).apply_patch(diff)

    assert file_path.read_text() == "hello\nworld\n"


def test_git_manager_apply_patch_skips_empty_patch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    repo_dir = _repo_path(repo)
    file_path = repo_dir / "file.txt"
    file_path.write_text("hello\n")

    GitManager(repo).apply_patch("")

    assert file_path.read_text() == "hello\n"
