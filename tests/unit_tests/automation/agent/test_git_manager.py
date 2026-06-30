from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from git import GitCommandError, Repo

from automation.agent.git_manager import (
    GitManager,
    GitPushNetworkError,
    GitPushPermissionError,
    GitPushStaleError,
    RepoStatus,
    _is_push_stale_error_text,
    _shell_quote,
)
from automation.agent.middlewares.file_system import SandboxFileBackend
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
    assert gm.unique_branch_name("feature", ["main"]) == "feature"


def test_gen_unique_branch_name_raises_when_max_attempts_exceeded() -> None:
    gm, _ = _sandbox_manager()
    with pytest.raises(ValueError, match="max attempts reached 3"):
        gm.unique_branch_name("feature", ["feature", "feature-1", "feature-2"], max_attempts=3)


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
# push_head_to non-fast-forward recovery (integrate_on_reject)
# ---------------------------------------------------------------------------

# A real non-fast-forward push rejection (the remote branch advanced under the run, e.g. a
# dependabot force-push of its rebased PR branch).
_NON_FF_REJECT = (
    "To https://github.com/x/y.git\n"
    " ! [rejected]        HEAD -> b (fetch first)\n"
    "error: failed to push some refs to 'https://github.com/x/y.git'\n"
    "hint: Updates were rejected because the remote contains work that you do not have locally.\n"
)


def _issued(client: MagicMock) -> list[str]:
    """The single git command issued in each ``run_commands`` round-trip, in order."""
    return [call.args[1].commands[0] for call in client.run_commands.await_args_list]


async def test_push_head_to_integrates_remote_and_retries_on_non_fast_forward() -> None:
    # First push is rejected as non-fast-forward; the manager fetches + rebases onto the remote
    # tip and retries the push, which then succeeds — the agent's work is preserved.
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[_resp((_NON_FF_REJECT, 1)), _resp(("", 0)), _resp(("", 0)), _resp(("", 0))]
    )
    gm = GitManager.for_sandbox(_backend_for(client))

    assert await gm.push_head_to("b", integrate_on_reject=True) == "b"

    issued = _issued(client)
    assert issued[0].endswith("push origin HEAD:b")
    assert "fetch origin b" in issued[1]
    assert "rebase FETCH_HEAD" in issued[2]
    assert issued[3].endswith("push origin HEAD:b")


async def test_push_head_to_raises_stale_on_rebase_conflict() -> None:
    # The remote moved and its changes conflict with the agent's; the rebase fails, the manager
    # aborts it (restoring HEAD) and raises a typed stale error instead of leaving a half-rebase.
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[
            _resp((_NON_FF_REJECT, 1)),
            _resp(("", 0)),
            _resp(("CONFLICT (content): merge", 1)),
            _resp(("", 0)),
        ]
    )
    gm = GitManager.for_sandbox(_backend_for(client))

    with pytest.raises(GitPushStaleError):
        await gm.push_head_to("b", integrate_on_reject=True)

    issued = _issued(client)
    assert any("rebase --abort" in command for command in issued)
    # No retry push is attempted once the rebase is aborted.
    assert sum(command.endswith("push origin HEAD:b") for command in issued) == 1


async def test_push_head_to_raises_stale_when_retry_still_rejected() -> None:
    # The remote advanced again between our fetch and the retry push -> still non-fast-forward.
    # We do not loop forever; surface a typed stale error.
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[_resp((_NON_FF_REJECT, 1)), _resp(("", 0)), _resp(("", 0)), _resp((_NON_FF_REJECT, 1))]
    )
    gm = GitManager.for_sandbox(_backend_for(client))

    with pytest.raises(GitPushStaleError):
        await gm.push_head_to("b", integrate_on_reject=True)


async def test_push_head_to_classifies_non_fast_forward_as_stale_without_integration() -> None:
    # Default (integrate_on_reject=False, e.g. a fresh-branch push): a non-fast-forward rejection is
    # still classified as a typed stale error rather than a raw GitCommandError, and we never
    # fetch/rebase (there is no shared intent to add onto whatever sits on that ref).
    gm, client = _sandbox_manager({"push origin HEAD:b": (1, _NON_FF_REJECT)})
    with pytest.raises(GitPushStaleError):
        await gm.push_head_to("b")
    assert not client.ran("fetch origin")
    assert not client.ran("rebase")


async def test_push_head_to_auth_wins_over_stale_markers() -> None:
    # A rejection whose output carries BOTH a non-fast-forward marker AND a 403 must classify as an
    # auth failure, not a transient stale race — `_raise_for_push_failure` checks stale last, so a
    # real permission problem is never masked as "re-trigger me".
    gm, _ = _sandbox_manager({"push origin HEAD:b": (1, _NON_FF_REJECT + "\nThe requested URL returned error: 403")})
    with pytest.raises(GitPushPermissionError):
        await gm.push_head_to("b")


async def test_push_head_to_network_wins_over_stale_markers() -> None:
    # Likewise an unreachable-host failure must win over a co-occurring stale marker.
    gm, _ = _sandbox_manager({
        "push origin HEAD:b": (1, _NON_FF_REJECT + "\nfatal: unable to access: Could not resolve host: example.com")
    })
    with pytest.raises(GitPushNetworkError):
        await gm.push_head_to("b")


async def test_push_head_to_integrate_skips_fetch_when_failure_is_auth_not_stale() -> None:
    # integrate_on_reject is on, but the first push failed for auth (no non-ff marker): the
    # `_is_push_stale_error_text` gate must NOT fire, so no fetch/rebase round-trip happens — an
    # auth failure won't change after a fetch+rebase — and the typed auth error surfaces directly.
    gm, client = _sandbox_manager({"push origin HEAD:b": (128, "The requested URL returned error: 403")})
    with pytest.raises(GitPushPermissionError):
        await gm.push_head_to("b", integrate_on_reject=True)
    assert not client.ran("fetch origin")
    assert not client.ran("rebase")


async def test_push_head_to_integrate_classifies_fetch_failure() -> None:
    # A fetch failure during recovery is classified like a push failure (here: auth) so the typed,
    # actionable error is preserved instead of degrading to a raw GitCommandError. No rebase is
    # attempted and no retry push happens after a failed fetch.
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[_resp((_NON_FF_REJECT, 1)), _resp(("The requested URL returned error: 403", 128))]
    )
    gm = GitManager.for_sandbox(_backend_for(client))

    with pytest.raises(GitPushPermissionError):
        await gm.push_head_to("b", integrate_on_reject=True)

    issued = _issued(client)
    assert "fetch origin b" in issued[1]
    assert not any("rebase" in command for command in issued)
    assert sum(command.endswith("push origin HEAD:b") for command in issued) == 1


async def test_push_head_to_classifies_stale_when_abort_fails() -> None:
    # If `git rebase --abort` itself fails, HEAD can't be restored: the workspace is left mid-rebase.
    # We still raise a typed stale error (so the failure is surfaced, not a raw crash) but the abort
    # failure is logged at error level rather than silently swallowed.
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[
            _resp((_NON_FF_REJECT, 1)),
            _resp(("", 0)),
            _resp(("CONFLICT (content): merge", 1)),
            _resp(("fatal: could not abort", 1)),
        ]
    )
    gm = GitManager.for_sandbox(_backend_for(client))

    with pytest.raises(GitPushStaleError, match="inconsistent state"):
        await gm.push_head_to("b", integrate_on_reject=True)

    assert any("rebase --abort" in command for command in _issued(client))


async def test_push_head_to_force_skips_integration_on_non_fast_forward() -> None:
    # The `not force` guard: a forced push that still gets a non-ff rejection must never fetch/rebase
    # (force is the deliberate overwrite path); it is classified as stale directly.
    gm, client = _sandbox_manager({"push origin HEAD:b --force": (1, _NON_FF_REJECT)})
    with pytest.raises(GitPushStaleError):
        await gm.push_head_to("b", force=True, integrate_on_reject=True)
    assert not client.ran("fetch origin")
    assert not client.ran("rebase")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("hint: (fetch first)", True),
        ("error: failed to push some refs ... ! [rejected] (non-fast-forward)", True),
        ("hint: Updates were rejected because the remote contains work that you do not have locally.", True),
        ("hint: tip of your current branch is behind its remote counterpart.", True),
        ("fatal: some other push failure", False),
        ("The requested URL returned error: 403", False),
        ("fatal: unable to access: Could not resolve host: example.com", False),
    ],
)
def test_is_push_stale_error_text_matches_each_marker(output: str, expected: bool) -> None:
    # Lock each non-fast-forward marker individually (the aggregate-output tests above happen to carry
    # two markers at once), plus negatives so an auth/network/other failure never reads as stale.
    assert _is_push_stale_error_text(output) is expected


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


# ---------------------------------------------------------------------------
# get_changed_files (same scope as get_diff, names straight from git)
# ---------------------------------------------------------------------------


async def test_get_changed_files_local_includes_tracked_and_untracked(tmp_path: Path) -> None:
    """Names come from `diff --name-only` + `ls-files`, so paths with spaces are exact —
    the whole point of this method over diff-header parsing."""
    repo, _ = _init_repo_with_origin(tmp_path)
    repo_dir = tmp_path / "work"
    (repo_dir / "README.md").write_text("changed\n")
    (repo_dir / "my file.txt").write_text("with space\n")

    changed = await GitManager.for_local(repo).get_changed_files()

    assert "README.md" in changed
    assert "my file.txt" in changed


async def test_get_changed_files_local_empty_when_clean(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)
    assert await GitManager.for_local(repo).get_changed_files() == []


async def test_get_changed_files_sandbox_single_round_trip() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(return_value=_resp(("a.py\nb.py\n", 0), ("new.py\n", 0)))
    gm = GitManager.for_sandbox(_backend_for(client))

    changed = await gm.get_changed_files()

    assert changed == ["a.py", "b.py", "new.py"]
    assert client.run_commands.await_count == 1
    sent = client.run_commands.await_args.args[1]
    assert any("diff --name-only HEAD" in command for command in sent.commands)
    assert any("ls-files --others --exclude-standard" in command for command in sent.commands)


async def test_get_changed_files_raises_on_failure() -> None:
    gm, _ = _sandbox_manager({"diff --name-only HEAD": (128, "fatal: bad revision"), "ls-files": (0, "")})
    with pytest.raises(GitCommandError):
        await gm.get_changed_files()


async def test_get_diff_raises_on_ls_files_failure() -> None:
    gm, _ = _sandbox_manager({"ls-files": (128, "fatal: boom")})
    with pytest.raises(GitCommandError):
        await gm.get_diff()
