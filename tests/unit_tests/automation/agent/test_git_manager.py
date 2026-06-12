from __future__ import annotations

from typing import TYPE_CHECKING
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
from automation.agent.middlewares.file_system import SandboxFileBackend
from codebase.utils import apply_patch_to_dir
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse

if TYPE_CHECKING:
    from pathlib import Path

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
    backend = SandboxFileBackend(client=client)
    backend.bind_session("sid")
    return GitManager.for_sandbox(backend), client


# ---------------------------------------------------------------------------
# Constructor / mode validation
# ---------------------------------------------------------------------------


def test_requires_exactly_one_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        GitManager()
    repo = _init_repo(tmp_path)
    backend = SandboxFileBackend(client=FakeSandboxClient())
    backend.bind_session("sid")
    with pytest.raises(ValueError, match="exactly one"):
        GitManager(repo, sandbox_backend=backend)


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

    backend = SandboxFileBackend(client=FakeSandboxClient())
    backend.bind_session("sid")
    gm = GitManager.for_sandbox(backend)
    assert gm._sandbox_backend is backend
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

    backend = SandboxFileBackend(client=_EmptyClient())
    backend.bind_session("sid")
    gm = GitManager.for_sandbox(backend)
    with pytest.raises(RuntimeError, match="no result"):
        await gm.commit_all("msg")


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


async def test_status_snapshot_raises_on_no_index_hard_error() -> None:
    # `git diff --no-index` exit 1 = "differs" (kept); exit >1 is a genuine error and must raise
    # rather than be swallowed into the snapshot diff.
    gm, _ = _sandbox_manager({
        "diff origin/main": (0, ""),
        "ls-files --others": (0, "weird.bin\n"),
        "--no-index": (2, "fatal: something broke"),
    })
    with pytest.raises(GitCommandError):
        await gm.status_snapshot(base_branch="main", mr_source_branch=None)


async def test_push_head_to_auth_wins_over_network_markers() -> None:
    # Output mentions BOTH a resolve-host failure and a 403; auth must win (checked first).
    gm, _ = _sandbox_manager({
        "push origin HEAD:b": (128, "Could not resolve host: x ... The requested URL returned error: 403")
    })
    with pytest.raises(GitPushPermissionError):
        await gm.push_head_to("b")


# ---------------------------------------------------------------------------
# status_snapshot (batched publish reads, <=2 round-trips)
# ---------------------------------------------------------------------------


def _resp(*outputs_and_codes):
    return RunCommandsResponse(
        results=[RunCommandResult(command="git", exit_code=code, output=out) for out, code in outputs_and_codes]
    )


def _backend_for(client) -> SandboxFileBackend:
    backend = SandboxFileBackend(client=client)
    backend.bind_session("sess-1")
    return backend


async def test_status_snapshot_one_round_trip_when_clean() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(
        return_value=_resp(("", 0), ("", 0), ("", 0), ("abc\trefs/heads/main\n", 0), ("", 0))
    )
    gm = GitManager.for_sandbox(_backend_for(client))
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
    gm = GitManager.for_sandbox(_backend_for(client))
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert snap.dirty is True
    assert "new.py" in snap.diff
    assert snap.has_unpushed is False
    assert client.run_commands.await_count == 2


@pytest.mark.parametrize(
    "failing_index,command_fragment",
    [(0, "status --porcelain"), (1, "diff origin/main"), (2, "ls-files --others"), (3, "ls-remote --heads")],
)
async def test_status_snapshot_raises_on_batch_a_command_failure(failing_index: int, command_fragment: str) -> None:
    # A non-zero exit from any batch-A query must raise rather than parse to a misleading empty value
    # (e.g. a failing ls-remote parsing to [] would risk a colliding branch name).
    outputs = [("", 0), ("", 0), ("", 0), ("abc\trefs/heads/main\n", 0)]
    outputs[failing_index] = ("boom", 128)
    client = MagicMock()
    client.run_commands = AsyncMock(return_value=_resp(*outputs))
    gm = GitManager.for_sandbox(_backend_for(client))
    with pytest.raises(GitCommandError) as exc_info:
        await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert command_fragment in str(exc_info.value)
    # The failing check short-circuits before the untracked second round-trip.
    assert client.run_commands.await_count == 1


async def test_status_snapshot_has_unpushed_true_when_log_has_output() -> None:
    # mr_source_branch present and `git log origin/<src>..HEAD` returns commits -> has_unpushed True.
    client = MagicMock()
    client.run_commands = AsyncMock(
        return_value=_resp(("", 0), ("", 0), ("", 0), ("abc\trefs/heads/main\n", 0), ("abc123 commit\n", 0))
    )
    gm = GitManager.for_sandbox(_backend_for(client))
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch="feat/x")
    assert snap.has_unpushed is True


async def test_status_snapshot_treats_log_failure_as_unpushed() -> None:
    # A non-zero `git log origin/<src>..HEAD` (e.g. an unknown upstream ref) is treated as "all
    # unpushed" rather than raising, mirroring has_unpushed().
    client = MagicMock()
    client.run_commands = AsyncMock(
        return_value=_resp(("", 0), ("", 0), ("", 0), ("abc\trefs/heads/main\n", 0), ("fatal: bad revision", 128))
    )
    gm = GitManager.for_sandbox(_backend_for(client))
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch="feat/x")
    assert snap.has_unpushed is True


async def test_status_snapshot_raises_on_result_count_mismatch() -> None:
    # The sandbox returns one result per command; a short list is a wire anomaly, not a parse-to-empty.
    client = MagicMock()
    client.run_commands = AsyncMock(return_value=_resp(("", 0), ("", 0)))  # 2 results for 4 commands
    gm = GitManager.for_sandbox(_backend_for(client))
    with pytest.raises(RuntimeError, match="results for"):
        await gm.status_snapshot(base_branch="main", mr_source_branch=None)


# ---------------------------------------------------------------------------
# get_diff (working-tree patch vs a ref, incl. untracked — eval patch capture)
# ---------------------------------------------------------------------------


async def test_get_diff_local_includes_tracked_changes_and_untracked(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)
    repo_dir = tmp_path / "work"
    (repo_dir / "README.md").write_text("changed\n")
    (repo_dir / "new.py").write_text("print('hi')\n")

    diff = await GitManager.for_local(repo).get_diff()

    assert "a/README.md" in diff
    assert "+changed" in diff
    assert "new.py" in diff
    assert "+print('hi')" in diff
    assert diff.endswith("\n")


async def test_get_diff_local_empty_when_clean(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)
    assert await GitManager.for_local(repo).get_diff() == ""


async def test_get_diff_sandbox_single_round_trip_when_no_untracked() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(return_value=_resp(("diff --git a/x b/x\n", 0), ("", 0)))
    gm = GitManager.for_sandbox(_backend_for(client))

    diff = await gm.get_diff()

    assert diff == "diff --git a/x b/x\n"
    assert client.run_commands.await_count == 1
    sent = client.run_commands.await_args.args[1]
    assert any("diff HEAD" in command for command in sent.commands)
    assert any("ls-files --others --exclude-standard" in command for command in sent.commands)


async def test_get_diff_sandbox_folds_untracked_in_second_round_trip() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[
            _resp(("diff --git a/x b/x\n", 0), ("new.py\n", 0)),
            # `diff --no-index` exits 1 when it finds differences — expected, keep the output.
            _resp(("+++ b/new.py\n+hello\n", 1)),
        ]
    )
    gm = GitManager.for_sandbox(_backend_for(client))

    diff = await gm.get_diff()

    assert "diff --git a/x b/x" in diff
    assert "+++ b/new.py" in diff
    assert diff.endswith("\n")
    assert client.run_commands.await_count == 2


async def test_get_diff_diffs_against_given_ref() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(return_value=_resp(("", 0), ("", 0)))
    gm = GitManager.for_sandbox(_backend_for(client))

    await gm.get_diff("abc123")

    sent = client.run_commands.await_args.args[1]
    assert any("diff abc123" in command for command in sent.commands)


async def test_get_diff_raises_on_diff_failure() -> None:
    gm, _ = _sandbox_manager({"diff HEAD": (128, "fatal: bad revision"), "ls-files": (0, "")})
    with pytest.raises(GitCommandError):
        await gm.get_diff()


async def test_get_diff_raises_on_ls_files_failure() -> None:
    gm, _ = _sandbox_manager({"ls-files": (128, "fatal: boom")})
    with pytest.raises(GitCommandError):
        await gm.get_diff()
