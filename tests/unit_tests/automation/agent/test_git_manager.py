from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from git import GitCommandError, Repo
from unidiff import PatchSet

from automation.agent.git_manager import (
    GitManager,
    GitPushNetworkError,
    GitPushPermissionError,
    GitPushStaleError,
    PendingMerge,
    _is_push_stale_error_text,
)
from automation.agent.git_runners import GitResult, LocalGitRunner, SandboxGitRunner
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse
from tests.unit_tests.conftest import FakeSandboxClient, sandbox_backend_on

if TYPE_CHECKING:
    from automation.agent.workspace.sandbox_backend import SandboxFileBackend

# ---------------------------------------------------------------------------
# Real GitPython repos for LocalGitRunner
# ---------------------------------------------------------------------------


def _configure_repo_identity(repo: Repo) -> None:
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test User")
        writer.set_value("user", "email", "test@example.com")


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


def _sandbox_manager(responses: dict[str, tuple[int, str]] | None = None) -> tuple[GitManager, FakeSandboxClient]:
    client = FakeSandboxClient.opened(responses)
    backend = sandbox_backend_on(client, client.add_running_session("sid"))
    return GitManager(SandboxGitRunner(backend)), client


def test_gen_unique_branch_name_returns_original_when_available() -> None:
    gm, _ = _sandbox_manager()
    assert gm.unique_branch_name("feature", ["main"]) == "feature"


def test_gen_unique_branch_name_appends_random_suffix_on_collision() -> None:
    gm, _ = _sandbox_manager()
    result = gm.unique_branch_name("feature", ["feature"])
    assert result != "feature"
    assert result.startswith("feature-")
    assert result not in {"feature"}


def test_gen_unique_branch_name_never_returns_an_existing_name() -> None:
    gm, _ = _sandbox_manager()
    existing = ["feature", *(f"feature-{i}" for i in range(1, 20))]
    result = gm.unique_branch_name("feature", existing)
    assert result not in existing
    assert result.startswith("feature-")


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
        await GitManager(LocalGitRunner(repo)).commit_all("nothing staged")


# ---------------------------------------------------------------------------
# push_head_to (publish + failure classification)
# ---------------------------------------------------------------------------


async def test_local_push_publishes_head_to_the_origin_branch(tmp_path: Path) -> None:
    repo, origin_dir = _init_repo_with_origin(tmp_path)

    assert await GitManager(LocalGitRunner(repo)).push_head_to("feature") == "feature"

    assert "feature" in Repo(origin_dir).heads


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


async def test_status_snapshot_classifies_lsremote_auth_failure() -> None:
    gm, _ = _sandbox_manager({
        "ls-remote --heads origin": (128, "fatal: could not read Username for 'https://x': terminal prompts disabled")
    })
    with pytest.raises(GitPushPermissionError):
        await gm.status_snapshot(base_branch="main", mr_source_branch=None)


async def test_status_snapshot_classifies_lsremote_network_failure() -> None:
    # An unreachable remote on the same first network op must classify as GitPushNetworkError.
    gm, _ = _sandbox_manager({"ls-remote --heads origin": (128, "fatal: unable to access ... Could not resolve host")})
    with pytest.raises(GitPushNetworkError):
        await gm.status_snapshot(base_branch="main", mr_source_branch=None)


async def test_status_snapshot_non_transport_lsremote_failure_still_raises_git_command_error() -> None:
    # A non-auth/non-network ls-remote failure keeps the original raw GitCommandError (unchanged).
    gm, _ = _sandbox_manager({"ls-remote --heads origin": (128, "fatal: something entirely unexpected")})
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
        side_effect=[_resp((_NON_FF_REJECT, 1)), _resp(("", 0)), _resp(("", 0)), _resp(("", 0)), _resp(("", 0))]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))

    assert await gm.push_head_to("b", integrate_on_reject=True) == "b"

    issued = _issued(client)
    assert issued[0].endswith("push origin HEAD:b")
    assert "rev-list --merges HEAD --not --remotes=origin" in issued[1]
    assert "fetch origin b" in issued[2]
    assert "rebase FETCH_HEAD" in issued[3]
    assert issued[4].endswith("push origin HEAD:b")


@pytest.mark.parametrize(
    ("merges", "integrate", "abort"),
    [
        pytest.param("", "rebase FETCH_HEAD", "rebase --abort", id="rebase"),
        pytest.param("d" * 40 + "\n", "merge --no-edit FETCH_HEAD", "merge --abort", id="run-with-a-merge"),
    ],
)
async def test_push_head_to_aborts_a_conflicted_integration_and_raises_stale(
    merges: str, integrate: str, abort: str
) -> None:
    # The remote moved and its changes conflict with the agent's; the manager aborts the rebase or merge (restoring
    # HEAD) and raises a typed stale error instead of leaving it half-done.
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[
            _resp((_NON_FF_REJECT, 1)),
            _resp((merges, 0)),
            _resp(("", 0)),
            _resp(("CONFLICT (content): merge", 1)),
            _resp(("", 0)),
        ]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))

    with pytest.raises(GitPushStaleError, match="could not be integrated"):
        await gm.push_head_to("b", integrate_on_reject=True)

    issued = _issued(client)
    assert integrate in issued[3]
    assert abort in issued[4]
    assert sum(command.endswith("push origin HEAD:b") for command in issued) == 1


async def test_push_head_to_raises_stale_when_retry_still_rejected() -> None:
    # The remote advanced again between our fetch and the retry push -> still non-fast-forward.
    # We do not loop forever; surface a typed stale error.
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[
            _resp((_NON_FF_REJECT, 1)),
            _resp(("", 0)),
            _resp(("", 0)),
            _resp(("", 0)),
            _resp((_NON_FF_REJECT, 1)),
        ]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))

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
        side_effect=[_resp((_NON_FF_REJECT, 1)), _resp(("", 0)), _resp(("The requested URL returned error: 403", 128))]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))

    with pytest.raises(GitPushPermissionError):
        await gm.push_head_to("b", integrate_on_reject=True)

    issued = _issued(client)
    assert "fetch origin b" in issued[2]
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
            _resp(("", 0)),
            _resp(("CONFLICT (content): merge", 1)),
            _resp(("fatal: could not abort", 1)),
        ]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))

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
    return sandbox_backend_on(client, "sess-1")


async def test_status_snapshot_diffs_against_merge_base_in_batch_b() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[
            # batch A: status, merge-base -> SHA, ls-files, ls-remote, log
            _resp(("", 0), ("mb123\n", 0), ("", 0), ("abc\trefs/heads/main\n", 0), ("", 0)),
            # batch B: the working-tree diff against the merge-base SHA
            _resp(("", 0)),
        ]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch="feat/x")
    assert (snap.dirty, snap.diff, snap.remote_branches, snap.has_unpushed) == (False, "", ["main"], False)
    assert client.run_commands.await_count == 2
    batch_a = client.run_commands.await_args_list[0].args[1]
    assert len(batch_a.commands) == 5
    assert any("merge-base origin/main HEAD" in c for c in batch_a.commands)
    batch_b = client.run_commands.await_args_list[1].args[1]
    assert "diff mb123" in batch_b.commands[0]


async def test_status_snapshot_falls_back_to_tip_when_no_merge_base(caplog) -> None:
    """A merge-base exit 1 (unrelated histories) falls back to diffing origin/<base> tip, with a warning."""
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[_resp(("", 0), ("", 1), ("", 0), ("abc\trefs/heads/main\n", 0)), _resp(("", 0))]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    with caplog.at_level(logging.WARNING, logger="daiv.tools"):
        snap = await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert snap.diff == ""
    batch_b = client.run_commands.await_args_list[1].args[1]
    assert "diff origin/main" in batch_b.commands[0]
    assert "no common ancestor" in caplog.text


async def test_status_snapshot_falls_back_to_tip_on_empty_merge_base_output() -> None:
    """merge-base exit 0 but no SHA (anomalous) falls back to the tip rather than an empty ref."""
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[_resp(("", 0), ("", 0), ("", 0), ("abc\trefs/heads/main\n", 0)), _resp(("", 0))]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert snap.diff == ""
    batch_b = client.run_commands.await_args_list[1].args[1]
    assert "diff origin/main" in batch_b.commands[0]


async def test_status_snapshot_raises_on_merge_base_hard_failure() -> None:
    """A merge-base failure that is NOT "no common ancestor" (exit 128, bad/missing ref) must surface
    rather than be mislabeled as unrelated histories and swept into the tip fallback."""
    client = MagicMock()
    client.run_commands = AsyncMock(
        return_value=_resp(("", 0), ("fatal: Not a valid object name", 128), ("", 0), ("abc\trefs/heads/main\n", 0))
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    with pytest.raises(GitCommandError) as exc_info:
        await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert "merge-base origin/main HEAD" in str(exc_info.value)
    # Raises during batch A, before the batch-B diff round-trip.
    assert client.run_commands.await_count == 1


async def test_status_snapshot_raises_when_batch_b_main_diff_fails() -> None:
    """The main working-tree diff moved to batch B; a non-zero exit there must still raise."""
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[
            _resp(("", 0), ("mb123\n", 0), ("", 0), ("abc\trefs/heads/main\n", 0)),
            _resp(("fatal: bad object", 128)),
        ]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    with pytest.raises(GitCommandError) as exc_info:
        await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert "diff mb123" in str(exc_info.value)
    assert client.run_commands.await_count == 2


async def test_status_snapshot_folds_untracked_into_batch_b() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[
            _resp(("?? new.py\n", 0), ("mb123\n", 0), ("new.py\n", 0), ("abc\trefs/heads/main\n", 0)),
            _resp(("", 0), ("+++ b/new.py\n+hello\n", 1)),  # diff <mb>, then diff --no-index for new.py
        ]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert snap.dirty is True
    assert "new.py" in snap.diff
    assert client.run_commands.await_count == 2


# An empty (hunkless) untracked file sorted before a real one — the fold ordering that made the old
# blank-line join trip ``unidiff``. Built fresh per call so one test can't mutate another's fixture.
_UNTRACKED_EMPTY_THEN_REAL_FILES = ["a_empty.py", "b_real.py"]


def _untracked_empty_then_real() -> list[GitResult]:
    return [
        # empty file: header only, no @@ hunk (the section that trips unidiff when a blank follows)
        GitResult(
            exit_code=1, output="diff --git a/a_empty.py b/a_empty.py\nnew file mode 100644\nindex 0000000..e69de29\n"
        ),
        GitResult(
            exit_code=1,
            output=(
                "diff --git a/b_real.py b/b_real.py\nnew file mode 100644\nindex 0000000..17e3475\n"
                "--- /dev/null\n+++ b/b_real.py\n@@ -0,0 +1 @@\n+real\n"
            ),
        ),
    ]


def test_append_untracked_folds_empty_file_without_breaking_diff_parse() -> None:
    """An empty untracked file folds in as a *hunkless* ``diff --git`` section.

    The fold must not inject a blank line after it: a blank line following a hunkless section
    makes ``unidiff`` abort the whole parse with "Unexpected trailing newline character", which
    silently bypasses ``redact_diff_content``'s omit-pattern redaction (the diff then reaches the
    metadata model unredacted). The stitched output must match canonical ``git diff`` — file
    sections butted together with no blank line between them — so it stays parseable.
    """
    base = (
        "diff --git a/tracked.py b/tracked.py\n"
        "index ce01362..9a7a4b5 100644\n"
        "--- a/tracked.py\n"
        "+++ b/tracked.py\n"
        "@@ -1 +1,2 @@\n"
        " hello\n"
        "+changed\n"
    )

    diff = GitManager._append_untracked(base, _UNTRACKED_EMPTY_THEN_REAL_FILES, _untracked_empty_then_real())

    assert "\n\ndiff --git" not in diff  # no spurious blank line between file sections
    assert [patched.path for patched in PatchSet.from_string(diff)] == ["tracked.py", "a_empty.py", "b_real.py"]


def test_append_untracked_with_empty_base_has_no_leading_blank_line() -> None:
    """With no tracked changes the base diff is empty, so the first folded section must start the
    output cleanly — no leading blank line. (The ``if diff and ...`` guard skips the boundary newline
    on the first iteration; an unconditional ``\\n`` join would instead emit a leading blank line and,
    after the following hunkless section, break parsing.)
    """
    diff = GitManager._append_untracked("", _UNTRACKED_EMPTY_THEN_REAL_FILES, _untracked_empty_then_real())

    assert not diff.startswith("\n")
    assert [patched.path for patched in PatchSet.from_string(diff)] == ["a_empty.py", "b_real.py"]


@pytest.mark.parametrize(
    "failing_index,command_fragment", [(0, "status --porcelain"), (2, "ls-files --others"), (3, "ls-remote --heads")]
)
async def test_status_snapshot_raises_on_batch_a_command_failure(failing_index: int, command_fragment: str) -> None:
    # A non-zero exit from any *gated* batch-A query must raise rather than parse to a misleading
    # empty value. (merge-base at index 1 is intentionally tolerant and excluded here.)
    outputs = [("", 0), ("mb123\n", 0), ("", 0), ("abc\trefs/heads/main\n", 0)]
    outputs[failing_index] = ("boom", 128)
    client = MagicMock()
    client.run_commands = AsyncMock(return_value=_resp(*outputs))
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    with pytest.raises(GitCommandError) as exc_info:
        await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert command_fragment in str(exc_info.value)
    # The failing check short-circuits before the batch-B diff round-trip.
    assert client.run_commands.await_count == 1


async def test_status_snapshot_has_unpushed_true_when_log_has_output() -> None:
    # mr_source_branch present and `git log origin/<src>..HEAD` returns commits -> has_unpushed True.
    # Batch A: status, merge-base, ls-files, ls-remote, log. Batch B: diff <merge-base>.
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[
            _resp(("", 0), ("mb123\n", 0), ("", 0), ("abc\trefs/heads/main\n", 0), ("abc123 commit\n", 0)),
            _resp(("", 0)),
        ]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch="feat/x")
    assert snap.has_unpushed is True


async def test_status_snapshot_treats_log_failure_as_unpushed() -> None:
    # A non-zero `git log origin/<src>..HEAD` (e.g. an unknown upstream ref) is treated as "all
    # unpushed" rather than raising, mirroring has_unpushed().
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[
            _resp(("", 0), ("mb123\n", 0), ("", 0), ("abc\trefs/heads/main\n", 0), ("fatal: bad revision", 128)),
            _resp(("", 0)),
        ]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch="feat/x")
    assert snap.has_unpushed is True


# ---------------------------------------------------------------------------
# pending_merge (a merge the agent left for the publisher to commit)
# ---------------------------------------------------------------------------


def _commit_file(repo: Repo, filename: str, content: str) -> None:
    (Path(repo.working_tree_dir) / filename).write_text(content)
    repo.git.add("-A")
    repo.index.commit(f"change {filename}")


def _push_fork_of_main(repo: Repo, branch: str, files: dict[str, str]) -> None:
    """Push ``branch``, ``main`` plus one commit per file in ``files``, and leave only ``origin/<branch>`` behind."""
    repo.git.switch("-c", branch, "main")
    for filename, content in files.items():
        _commit_file(repo, filename, content)
    repo.git.push("origin", branch)
    repo.git.switch("main")
    repo.git.branch("-D", branch)


def _start_merge(repo: Repo, ref: str) -> None:
    repo.git.merge("--no-commit", "--no-ff", ref, with_exceptions=False)


async def test_pending_merge_is_none_without_a_merge(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)

    assert await GitManager(LocalGitRunner(repo)).pending_merge() is None


async def test_pending_merge_lists_the_files_left_with_conflict_markers(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)
    _push_fork_of_main(repo, "other", {"README.md": "theirs\n"})
    _commit_file(repo, "README.md", "ours\n")
    _start_merge(repo, "origin/other")

    pending = await GitManager(LocalGitRunner(repo)).pending_merge()

    assert pending == PendingMerge(
        head=repo.commit("origin/other").hexsha,
        branch="remotes/origin/other",
        unmerged_paths=("README.md",),
        conflicted_paths=("README.md",),
    )


async def test_pending_merge_names_the_merged_branch_rather_than_origin_head_or_a_tag(tmp_path: Path) -> None:
    """A clone's ``origin/HEAD``, and a release tag, point at the default branch's tip too."""
    repo, origin_dir = _init_repo_with_origin(tmp_path)
    repo.git.push("origin", "main:feature")
    _commit_file(repo, "theirs.txt", "theirs\n")
    repo.git.tag("v1")
    repo.git.push("origin", "main", "v1")
    Repo(origin_dir).git.symbolic_ref("HEAD", "refs/heads/main")
    clone = Repo.clone_from(origin_dir.as_posix(), tmp_path / "clone", branch="feature")
    _configure_repo_identity(clone)
    clone.git.rev_parse("--verify", "refs/remotes/origin/HEAD")
    _commit_file(clone, "mine.txt", "mine\n")
    _start_merge(clone, "origin/main")

    pending = await GitManager(LocalGitRunner(clone)).pending_merge()

    assert pending is not None
    assert pending.branch == "remotes/origin/main"


async def test_pending_merge_of_a_commit_no_branch_ends_at_names_no_branch(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)
    _push_fork_of_main(repo, "other", {"a.txt": "a\n", "b.txt": "b\n"})
    _commit_file(repo, "mine.txt", "mine\n")
    _start_merge(repo, "origin/other~1")

    pending = await GitManager(LocalGitRunner(repo)).pending_merge()

    assert pending is not None
    assert pending.branch is None


@pytest.mark.parametrize(
    ("branch", "into", "expected"),
    [
        pytest.param("remotes/origin/main", "feature", "Merge remote-tracking branch 'origin/main' into feature"),
        pytest.param("main", "feature", "Merge branch 'main' into feature"),
        pytest.param(None, "feature", f"Merge commit '{'b' * 40}' into feature"),
        pytest.param("remotes/origin/main", None, "Merge remote-tracking branch 'origin/main'"),
    ],
)
def test_pending_merge_commit_subject_matches_git(branch: str | None, into: str | None, expected: str) -> None:
    merge = PendingMerge(head="b" * 40, branch=branch, unmerged_paths=(), conflicted_paths=())

    assert merge.commit_subject(into) == expected


async def test_pending_merge_raises_when_merge_head_cannot_be_read() -> None:
    """Only "no such ref" means no merge: anything else would commit the merge unchecked, as a plain commit."""
    gm, _ = _sandbox_manager({"rev-parse -q --verify MERGE_HEAD": (128, "fatal: not a git repository")})

    with pytest.raises(GitCommandError):
        await gm.pending_merge()


async def test_pending_merge_reads_through_git_warnings() -> None:
    sha = "c" * 40
    gm, _ = _sandbox_manager({
        "rev-parse -q --verify MERGE_HEAD": (0, f"warning: unable to access '/root/.gitconfig'\n{sha}\n"),
        "ls-files -u -z": (0, "100644 aaa 2\ta.py\x00100644 bbb 3\ta.py\x00100644 ccc 2\tb.py\x00"),
        "grep -l -z": (0, "warning: unable to access '/root/.gitconfig'\na.py\x00"),
    })

    pending = await gm.pending_merge()

    assert pending is not None
    assert (pending.head, pending.unmerged_paths, pending.conflicted_paths) == (sha, ("a.py", "b.py"), ("a.py",))


async def test_pending_merge_raises_when_the_marker_check_fails() -> None:
    gm, _ = _sandbox_manager({
        "rev-parse -q --verify MERGE_HEAD": (0, "c" * 40),
        "ls-files -u -z": (0, "100644 aaa 2\ta.py\x00"),
        "grep -l -z": (2, "fatal: cannot read a.py"),
    })

    with pytest.raises(GitCommandError):
        await gm.pending_merge()


async def test_pending_merge_reports_no_conflicts_once_the_markers_are_gone(tmp_path: Path) -> None:
    """The agent cannot stage, so a resolved file is still unmerged in the index; only its markers tell. A heading
    underline of seven ``=`` is not one."""
    repo, _ = _init_repo_with_origin(tmp_path)
    repo_dir = tmp_path / "work"
    _push_fork_of_main(repo, "other", {"README.md": "theirs\n"})
    _commit_file(repo, "README.md", "ours\n")
    _start_merge(repo, "origin/other")
    (repo_dir / "README.md").write_text("Install\n=======\n\nours and theirs\n")

    pending = await GitManager(LocalGitRunner(repo)).pending_merge()

    assert pending is not None
    assert (pending.unmerged_paths, pending.conflicted_paths) == (("README.md",), ())


async def test_status_snapshot_of_a_pending_merge_leaves_the_merged_branch_out_of_the_diff(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)
    _push_fork_of_main(repo, "target", {"theirs.txt": "theirs\n"})
    _commit_file(repo, "mine.txt", "mine\n")
    _start_merge(repo, "origin/target")
    gm = GitManager(LocalGitRunner(repo))
    pending = await gm.pending_merge()
    assert pending is not None

    snap = await gm.status_snapshot(base_branch="target", mr_source_branch=None, merge_head=pending.head)

    assert "mine.txt" in snap.diff
    assert "theirs.txt" not in snap.diff


async def test_integrating_a_rejected_push_keeps_the_merged_branch_commits(tmp_path: Path) -> None:
    """A rebase would re-create the merged branch's commits under new shas, so the MR would list them again."""
    repo, origin_dir = _init_repo_with_origin(tmp_path)
    _push_fork_of_main(repo, "target", {"theirs.txt": "theirs\n"})
    repo.git.push("origin", "main:feature")
    repo.git.switch("-c", "feature", "--track", "origin/feature")
    _start_merge(repo, "origin/target")
    await GitManager(LocalGitRunner(repo)).commit_all("Merge branch 'origin/target' into feature")
    merge_commit = repo.head.commit
    other = Repo.clone_from(origin_dir.as_posix(), tmp_path / "other", branch="feature")
    _configure_repo_identity(other)
    _commit_file(other, "concurrent.txt", "pushed meanwhile\n")
    other.git.push("origin", "feature")

    await GitManager(LocalGitRunner(repo)).push_head_to("feature", integrate_on_reject=True)

    tip = Repo(origin_dir).commit("feature")
    assert [parent.hexsha for parent in tip.parents] == [merge_commit.hexsha, other.head.commit.hexsha]
    assert merge_commit.parents[1].hexsha == repo.commit("origin/target").hexsha


async def test_integrating_a_force_pushed_branch_rebases_when_only_the_old_history_had_a_merge(tmp_path: Path) -> None:
    """Merging would bring the commits the force-push rewrote back in; only the run's own commits decide."""
    repo, origin_dir = _init_repo_with_origin(tmp_path)
    _push_fork_of_main(repo, "target", {"theirs.txt": "theirs\n"})
    repo.git.switch("-c", "feature")
    repo.git.merge("--no-ff", "-m", "Merge target", "origin/target")
    repo.git.push("origin", "feature")
    _commit_file(repo, "mine.txt", "mine\n")
    other = Repo.clone_from(origin_dir.as_posix(), tmp_path / "other")
    _configure_repo_identity(other)
    other.git.switch("-c", "rewritten", "origin/main")
    other.git.cherry_pick("-x", other.commit("origin/target").hexsha)
    other.git.push("--force", "origin", "rewritten:feature")

    await GitManager(LocalGitRunner(repo)).push_head_to("feature", integrate_on_reject=True)

    tip = Repo(origin_dir).commit("feature")
    assert [parent.hexsha for parent in tip.parents] == [other.head.commit.hexsha]


# ---------------------------------------------------------------------------
# get_diff (working-tree patch vs a ref, incl. untracked — eval patch capture)
# ---------------------------------------------------------------------------


async def test_get_diff_local_includes_tracked_changes_and_untracked(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)
    repo_dir = tmp_path / "work"
    (repo_dir / "README.md").write_text("changed\n")
    (repo_dir / "new.py").write_text("print('hi')\n")

    diff = await GitManager(LocalGitRunner(repo)).get_diff()

    assert "a/README.md" in diff
    assert "+changed" in diff
    assert "new.py" in diff
    assert "+print('hi')" in diff
    assert diff.endswith("\n")


async def test_get_diff_local_empty_when_clean(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)
    assert await GitManager(LocalGitRunner(repo)).get_diff() == ""


async def test_get_diff_sandbox_single_round_trip_when_no_untracked() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(return_value=_resp(("diff --git a/x b/x\n", 0), ("", 0)))
    gm = GitManager(SandboxGitRunner(_backend_for(client)))

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
    gm = GitManager(SandboxGitRunner(_backend_for(client)))

    diff = await gm.get_diff()

    assert "diff --git a/x b/x" in diff
    assert "+++ b/new.py" in diff
    assert diff.endswith("\n")
    assert client.run_commands.await_count == 2


async def test_get_diff_diffs_against_given_ref() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(return_value=_resp(("", 0), ("", 0)))
    gm = GitManager(SandboxGitRunner(_backend_for(client)))

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

    changed = await GitManager(LocalGitRunner(repo)).get_changed_files()

    assert "README.md" in changed
    assert "my file.txt" in changed


async def test_get_changed_files_local_empty_when_clean(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)
    assert await GitManager(LocalGitRunner(repo)).get_changed_files() == []


async def test_get_changed_files_sandbox_single_round_trip() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(return_value=_resp(("a.py\nb.py\n", 0), ("new.py\n", 0)))
    gm = GitManager(SandboxGitRunner(_backend_for(client)))

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


async def test_push_head_to_adds_ci_skip_push_option_when_skip_ci() -> None:
    gm, client = _sandbox_manager()
    await gm.push_head_to("b", skip_ci=True)
    push_cmds = [c for c in client.commands if " push " in f" {c} "]
    assert push_cmds and all("-o ci.skip" in c for c in push_cmds)


async def test_push_head_to_omits_ci_skip_by_default() -> None:
    gm, client = _sandbox_manager()
    await gm.push_head_to("b")
    assert not any("ci.skip" in c for c in client.commands)


async def test_push_head_to_skip_ci_survives_the_integrate_on_reject_retry() -> None:
    # Heal path: an existing MR's ephemeral-token push is skip-ci'd and may hit a non-ff rejection.
    # `-o ci.skip` must ride the rebase-retry push too, or the bot's doomed pipeline fires on the
    # second attempt and silently defeats the heal.
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[_resp((_NON_FF_REJECT, 1)), _resp(("", 0)), _resp(("", 0)), _resp(("", 0)), _resp(("", 0))]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))

    assert await gm.push_head_to("b", integrate_on_reject=True, skip_ci=True) == "b"

    pushes = [c for c in _issued(client) if "origin HEAD:b" in c]
    assert len(pushes) == 2  # first push + rebase-retry push
    assert all("-o ci.skip" in c for c in pushes)


async def test_head_sha_reads_the_current_commit() -> None:
    gm, client = _sandbox_manager({"rev-parse HEAD": (0, "2e5298e41d45a0a919ce01ec9c81714fc3440fda\n")})
    assert await gm.head_sha() == "2e5298e41d45a0a919ce01ec9c81714fc3440fda"
    assert client.ran("rev-parse HEAD")


async def test_head_sha_ignores_a_git_warning_printed_before_the_sha() -> None:
    # `output` is stdout+stderr, so a warning would otherwise be returned as part of the sha and
    # every downstream comparison against the remote would fail on a healthy repo.
    gm, _ = _sandbox_manager({
        "rev-parse HEAD": (0, "warning: unable to access '/root/.gitconfig'\n" + "a" * 40 + "\n")
    })
    assert await gm.head_sha() == "a" * 40


async def test_head_sha_refuses_output_that_is_not_a_sha() -> None:
    gm, _ = _sandbox_manager({"rev-parse HEAD": (0, "HEAD\n")})
    with pytest.raises(GitCommandError):
        await gm.head_sha()
