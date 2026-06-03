from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from git import GitCommandError, Repo

from automation.agent.git_manager import (
    GitManager,
    GitPushNetworkError,
    GitPushPermissionError,
    RepoStatus,
    _shell_quote,
)
from codebase.utils import apply_patch_to_dir
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse

# ---------------------------------------------------------------------------
# Local-mode helpers (GitPython clone; sandbox-disabled / repoless runs)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Sandbox-mode test double
# ---------------------------------------------------------------------------


class FakeSandboxClient:
    """Records issued git commands and returns canned ``(exit_code, output)`` results.

    ``responses`` maps a substring of the command to ``(exit_code, output)``; the first
    matching entry wins. Unmatched commands return success with empty output.
    """

    def __init__(self, responses: dict[str, tuple[int, str]] | None = None) -> None:
        self.responses = responses or {}
        self.commands: list[str] = []

    async def run_commands(self, session_id, request) -> RunCommandsResponse:  # noqa: ARG002
        results: list[RunCommandResult] = []
        for command in request.commands:
            self.commands.append(command)
            exit_code, output = 0, ""
            for needle, (code, out) in self.responses.items():
                if needle in command:
                    exit_code, output = code, out
                    break
            results.append(RunCommandResult(command=command, output=output, exit_code=exit_code))
        return RunCommandsResponse(results=results)

    def ran(self, needle: str) -> bool:
        return any(needle in command for command in self.commands)


def _sandbox_manager(responses: dict[str, tuple[int, str]] | None = None) -> tuple[GitManager, FakeSandboxClient]:
    client = FakeSandboxClient(responses)
    return GitManager(client=client, session_id="sid"), client


# ---------------------------------------------------------------------------
# Constructor / mode validation
# ---------------------------------------------------------------------------


def test_requires_exactly_one_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        GitManager()
    repo = _init_repo(tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        GitManager(repo, client=FakeSandboxClient())  # type: ignore[arg-type]


def test_sandbox_mode_requires_session_id() -> None:
    with pytest.raises(ValueError, match="session_id"):
        GitManager(client=FakeSandboxClient(), session_id="")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Local mode (GitPython clone)
# ---------------------------------------------------------------------------


async def test_local_is_dirty_detects_new_untracked_files(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = Repo.init(repo_dir)
    (repo_dir / "new_file.txt").write_text("hello\n")

    assert await GitManager(repo).is_dirty() is True


async def test_local_is_dirty_returns_false_for_clean_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    repo_dir = _repo_path(repo)
    (repo_dir / "README.md").write_text("hello\n")
    repo.git.add("-A")
    repo.index.commit("Initial commit")

    assert await GitManager(repo).is_dirty() is False


async def test_local_get_diff_includes_untracked_file_diff(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    Repo.init(repo_dir)
    (repo_dir / "new_file.txt").write_text("hello\n")

    diff = await GitManager(Repo(repo_dir)).get_diff()
    # For untracked files we add `git diff --no-index /dev/null <file>`, which includes the path.
    assert "new_file.txt" in diff


# ---------------------------------------------------------------------------
# Sandbox mode (git via run_commands)
# ---------------------------------------------------------------------------


async def test_sandbox_is_dirty_uses_status_porcelain() -> None:
    gm, client = _sandbox_manager({"status --porcelain": (0, " M a.py\n")})
    assert await gm.is_dirty() is True
    assert client.ran("git -C /workspace/repo status --porcelain")


async def test_sandbox_is_dirty_false_on_clean_tree() -> None:
    gm, _ = _sandbox_manager({"status --porcelain": (0, "")})
    assert await gm.is_dirty() is False


async def test_sandbox_get_diff_appends_untracked_files() -> None:
    gm, client = _sandbox_manager({
        "diff HEAD": (0, "diff --git a/x b/x\n"),
        "ls-files --others": (0, "new.py\n"),
        "--no-index": (1, "diff --git a/new.py b/new.py\n+added\n"),
    })
    diff = await gm.get_diff()
    assert "a/x" in diff
    assert "new.py" in diff
    assert client.ran("git -C /workspace/repo diff HEAD")
    assert client.ran("ls-files --others --exclude-standard")


async def test_sandbox_has_unpushed() -> None:
    gm, _ = _sandbox_manager({"log origin/main..HEAD": (0, "abc123 commit\n")})
    assert await gm.has_unpushed("main") is True
    gm2, _ = _sandbox_manager({"log origin/main..HEAD": (0, "")})
    assert await gm2.has_unpushed("main") is False


# ---------------------------------------------------------------------------
# Pure branch-name logic (mode-independent)
# ---------------------------------------------------------------------------


def test_gen_unique_branch_name_returns_original_when_available() -> None:
    gm, _ = _sandbox_manager()
    assert gm._gen_unique_branch_name("feature", ["main"]) == "feature"


def test_gen_unique_branch_name_raises_when_max_attempts_exceeded() -> None:
    gm, _ = _sandbox_manager()
    with pytest.raises(ValueError, match="max attempts reached 3"):
        gm._gen_unique_branch_name("feature", ["feature", "feature-1", "feature-2"], max_attempts=3)


# ---------------------------------------------------------------------------
# apply_patch_to_dir (repoless helper, unchanged)
# ---------------------------------------------------------------------------


def test_apply_patch_to_dir_works_without_git_repo(tmp_path: Path) -> None:
    """Repoless agent runs use ``apply_patch_to_dir`` directly against the on-disk working
    directory; ``git apply`` does not require a ``.git`` folder, so the patch must apply
    cleanly even when ``tmp_path`` is just a plain dir."""
    file_path = tmp_path / "file.txt"
    file_path.write_text("hello\n")

    diff = "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n@@ -1 +1,2 @@\n hello\n+world\n"

    apply_patch_to_dir(diff, tmp_path)

    assert file_path.read_text() == "hello\nworld\n"
    assert not (tmp_path / ".git").exists()


def test_apply_patch_to_dir_skips_empty_patch(tmp_path: Path) -> None:
    apply_patch_to_dir("", tmp_path)
    apply_patch_to_dir("   \n", tmp_path)


def test_apply_patch_to_dir_skips_non_patch_input_via_git_sentinel(tmp_path: Path) -> None:
    """Non-whitespace text that ``git apply`` rejects with "No valid patches in input"
    must be treated as a no-op, not a failure — covers the stderr-sentinel branch
    that the up-front strip() short-circuit doesn't reach."""
    apply_patch_to_dir("this is not a patch\n", tmp_path)


def test_apply_patch_to_dir_creates_new_file_in_non_repo_dir(tmp_path: Path) -> None:
    """``git apply`` over stdin must create new files in a plain working directory
    (no ``.git/``)."""
    new_file_diff = (
        "diff --git a/new.txt b/new.txt\n"
        "new file mode 100644\n"
        "index 0000000..3b18e51\n"
        "--- /dev/null\n"
        "+++ b/new.txt\n"
        "@@ -0,0 +1 @@\n"
        "+hello world\n"
    )

    apply_patch_to_dir(new_file_diff, tmp_path)

    assert (tmp_path / "new.txt").read_text() == "hello world\n"
    assert not (tmp_path / ".git").exists()


def test_apply_patch_to_dir_raises_runtime_error_on_invalid_patch(tmp_path: Path) -> None:
    """Malformed patches must surface as ``RuntimeError`` so callers can fail loud."""
    bogus_diff = (
        "diff --git a/missing.txt b/missing.txt\n--- a/missing.txt\n+++ b/missing.txt\n@@ -1 +1 @@\n-was here\n+gone\n"
    )

    with pytest.raises(RuntimeError, match="git apply"):
        apply_patch_to_dir(bogus_diff, tmp_path)


# ---------------------------------------------------------------------------
# Constructors / mode validation (classmethods)
# ---------------------------------------------------------------------------


def test_classmethod_constructors(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    assert GitManager.for_local(repo).repo is repo

    gm = GitManager.for_sandbox(FakeSandboxClient(), "sid")  # type: ignore[arg-type]
    assert gm._session_id == "sid"
    assert gm.repo is None


# ---------------------------------------------------------------------------
# _shell_quote (sandbox command construction)
# ---------------------------------------------------------------------------


def test_shell_quote_passes_safe_args_through() -> None:
    assert _shell_quote("status") == "status"
    assert _shell_quote("--porcelain") == "--porcelain"
    assert _shell_quote("origin/main..HEAD") == "origin/main..HEAD"


def test_shell_quote_single_quotes_args_with_spaces() -> None:
    assert _shell_quote("fix: thing") == "'fix: thing'"


def test_shell_quote_escapes_embedded_single_quote() -> None:
    # The POSIX idiom: close the quote, emit an escaped quote, reopen — `'\''`.
    assert _shell_quote("don't") == "'don'\\''t'"


def test_shell_quote_preserves_newlines_inside_quotes() -> None:
    quoted = _shell_quote("line1\nline2")
    assert quoted.startswith("'") and quoted.endswith("'")
    assert "\n" in quoted


async def test_shell_quote_applied_to_commit_message_with_apostrophe() -> None:
    gm, client = _sandbox_manager()
    await gm.commit_all("fix: don't break")
    assert client.ran("commit -m 'fix: don'\\''t break'")


# ---------------------------------------------------------------------------
# _git error-propagation contract (check=True must raise)
# ---------------------------------------------------------------------------


async def test_sandbox_git_check_raises_on_nonzero_exit() -> None:
    gm, _ = _sandbox_manager({"add -A": (1, "fatal: boom")})
    with pytest.raises(GitCommandError):
        await gm.commit_all("msg")


async def test_local_git_check_raises_on_nonzero_exit(tmp_path: Path) -> None:
    # Clean tree -> `git commit` exits non-zero ("nothing to commit"); check=True must raise.
    repo, _ = _init_repo_with_origin(tmp_path)
    with pytest.raises(GitCommandError):
        await GitManager(repo).commit_all("nothing staged")


async def test_sandbox_empty_results_raises_runtime_error() -> None:
    class _EmptyClient:
        async def run_commands(self, session_id, request) -> RunCommandsResponse:  # noqa: ARG002
            return RunCommandsResponse(results=[])

    gm = GitManager(client=_EmptyClient(), session_id="sid")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="no result"):
        await gm.is_dirty()


# ---------------------------------------------------------------------------
# get_diff error handling (no error text folded into the diff)
# ---------------------------------------------------------------------------


async def test_sandbox_get_diff_raises_on_missing_ref() -> None:
    gm, _ = _sandbox_manager({"diff origin/main": (128, "fatal: ambiguous argument 'origin/main': unknown revision")})
    with pytest.raises(GitCommandError):
        await gm.get_diff("origin/main")


async def test_sandbox_get_diff_falls_back_to_staged_diff_for_empty_repo() -> None:
    # `ref="HEAD"` with no commits: fall back to the staged diff rather than raising.
    gm, client = _sandbox_manager({
        "diff HEAD": (128, "fatal: bad revision 'HEAD'"),
        "diff --cached": (0, "staged diff\n"),
    })
    diff = await gm.get_diff("HEAD")
    assert "staged diff" in diff
    assert client.ran("diff --cached --no-prefix")


# ---------------------------------------------------------------------------
# has_unpushed (missing upstream ref must not masquerade as commit output)
# ---------------------------------------------------------------------------


async def test_sandbox_has_unpushed_treats_missing_ref_as_unpushed() -> None:
    gm, _ = _sandbox_manager({"log origin/new..HEAD": (128, "fatal: ambiguous argument 'origin/new..HEAD'")})
    assert await gm.has_unpushed("new") is True


# ---------------------------------------------------------------------------
# push_head_to failure classification
# ---------------------------------------------------------------------------


async def test_push_head_to_raises_permission_error_on_auth_failure() -> None:
    gm, _ = _sandbox_manager({"push origin HEAD:b": (128, "...The requested URL returned error: 403")})
    with pytest.raises(GitPushPermissionError):
        await gm.push_head_to("b")


async def test_push_head_to_raises_network_error_on_unreachable_host() -> None:
    gm, _ = _sandbox_manager({
        "push origin HEAD:b": (128, "fatal: unable to access 'https://...': Could not resolve host: gitlab.example.com")
    })
    with pytest.raises(GitPushNetworkError):
        await gm.push_head_to("b")


async def test_push_head_to_raises_git_command_error_on_other_failure() -> None:
    gm, _ = _sandbox_manager({"push origin HEAD:b": (1, "fatal: some other push failure")})
    with pytest.raises(GitCommandError):
        await gm.push_head_to("b")


# ---------------------------------------------------------------------------
# _parse_remote_branches (ls-remote line parsing)
# ---------------------------------------------------------------------------


def test_parse_remote_branches_filters_non_heads() -> None:
    out = "deadbeef\trefs/heads/main\ncafef00d\trefs/tags/v1\nbeef\trefs/heads/feature\n"
    assert GitManager._parse_remote_branches(out) == ["main", "feature"]


async def test_get_diff_raises_on_no_index_hard_error() -> None:
    # `git diff --no-index` exit 1 = "differs" (kept); exit >1 is a genuine error and must raise
    # rather than be swallowed into the diff.
    gm, _ = _sandbox_manager({
        "diff HEAD": (0, ""),
        "ls-files --others": (0, "weird.bin\n"),
        "--no-index": (2, "fatal: something broke"),
    })
    with pytest.raises(GitCommandError):
        await gm.get_diff()


async def test_push_head_to_auth_wins_over_network_markers() -> None:
    # Output mentions BOTH a resolve-host failure and a 403; auth must win (checked first).
    gm, _ = _sandbox_manager({
        "push origin HEAD:b": (128, "Could not resolve host: x ... The requested URL returned error: 403")
    })
    with pytest.raises(GitPushPermissionError):
        await gm.push_head_to("b")


async def test_remote_branches_raises_on_ls_remote_failure() -> None:
    # A failing ls-remote must raise, not parse to [] (which would risk a colliding branch name).
    gm, _ = _sandbox_manager({"ls-remote --heads": (128, "fatal: could not read from remote repository")})
    with pytest.raises(GitCommandError):
        await gm.remote_branches()


# ---------------------------------------------------------------------------
# status_snapshot (batched publish reads, <=2 round-trips)
# ---------------------------------------------------------------------------


def _resp(*outputs_and_codes):
    return RunCommandsResponse(
        results=[RunCommandResult(command="git", exit_code=code, output=out) for out, code in outputs_and_codes]
    )


async def test_status_snapshot_one_round_trip_when_clean() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(
        return_value=_resp(("", 0), ("", 0), ("", 0), ("abc\trefs/heads/main\n", 0), ("", 0))
    )
    gm = GitManager.for_sandbox(client, "sess-1")
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch="feat/x")
    assert isinstance(snap, RepoStatus)
    assert (snap.dirty, snap.diff, snap.remote_branches, snap.has_unpushed) == (False, "", ["main"], False)
    assert client.run_commands.await_count == 1
    sent = client.run_commands.await_args.args[1]
    assert sent.fail_fast is False
    assert len(sent.commands) == 5


async def test_status_snapshot_second_round_trip_only_for_untracked() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[
            _resp(("?? new.py\n", 0), ("", 0), ("new.py\n", 0), ("abc\trefs/heads/main\n", 0)),
            _resp(("+++ b/new.py\n+hello\n", 1)),
        ]
    )
    gm = GitManager.for_sandbox(client, "sess-1")
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert snap.dirty is True
    assert "new.py" in snap.diff
    assert snap.has_unpushed is False
    assert client.run_commands.await_count == 2
