from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from git import GitCommandError, Repo
from unidiff import PatchSet

from automation.agent.workspace.git_manager import (
    GitManager,
    GitPushNetworkError,
    GitPushPermissionError,
    GitPushStaleError,
    PendingMerge,
    _is_push_stale_error_text,
)
from automation.agent.workspace.git_runners import GitResult, LocalGitRunner, SandboxGitRunner
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


async def test_push_head_to_names_the_missing_workflows_permission() -> None:
    gm, _ = _sandbox_manager({
        "push origin HEAD:b": (
            1,
            "To https://github.com/x/y.git\n"
            " ! [remote rejected]   HEAD -> b (refusing to allow a GitHub App to create or update workflow "
            "`.github/workflows/ci.yml` without `workflows` permission)\n"
            "error: failed to push some refs to 'https://github.com/x/y.git'\n",
        )
    })
    with pytest.raises(GitPushPermissionError, match=r"Workflows.*Read and write"):
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


def _batch_a(
    *,
    status: str = "",
    merge_base: tuple[str, int] = ("mb123\n", 0),
    untracked: str = "",
    log: tuple[str, int] | None = None,
) -> list[tuple[str, int]]:
    """Batch A's results in command order: status, merge-base, ls-files, ls-remote, rev-parse HEAD, then the MR log."""
    outputs = [(status, 0), merge_base, (untracked, 0), ("abc\trefs/heads/main\n", 0), (FakeSandboxClient.HEAD_SHA, 0)]
    return [*outputs, log] if log else outputs


def _backend_for(client) -> SandboxFileBackend:
    return sandbox_backend_on(client, "sess-1")


async def test_status_snapshot_diffs_against_merge_base_in_batch_b() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[
            _resp(*_batch_a(log=("", 0))),
            # batch B: the working-tree diff against the merge-base SHA
            _resp(("", 0)),
        ]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch="feat/x")
    assert (snap.dirty, snap.diff, snap.remote_branches, snap.has_unpushed) == (False, "", ["main"], False)
    assert client.run_commands.await_count == 2
    batch_a = client.run_commands.await_args_list[0].args[1]
    assert len(batch_a.commands) == 6
    assert any("merge-base origin/main HEAD" in c for c in batch_a.commands)
    batch_b = client.run_commands.await_args_list[1].args[1]
    assert "diff mb123" in batch_b.commands[0]


async def test_status_snapshot_falls_back_to_tip_when_no_merge_base(caplog) -> None:
    """A merge-base exit 1 (unrelated histories) falls back to diffing origin/<base> tip, with a warning."""
    client = MagicMock()
    client.run_commands = AsyncMock(side_effect=[_resp(*_batch_a(merge_base=("", 1))), _resp(("", 0))])
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
    client.run_commands = AsyncMock(side_effect=[_resp(*_batch_a(merge_base=("", 0))), _resp(("", 0))])
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert snap.diff == ""
    batch_b = client.run_commands.await_args_list[1].args[1]
    assert "diff origin/main" in batch_b.commands[0]


async def test_status_snapshot_raises_on_merge_base_hard_failure() -> None:
    """A merge-base failure that is NOT "no common ancestor" (exit 128, bad/missing ref) must surface
    rather than be mislabeled as unrelated histories and swept into the tip fallback."""
    client = MagicMock()
    client.run_commands = AsyncMock(return_value=_resp(*_batch_a(merge_base=("fatal: Not a valid object name", 128))))
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    with pytest.raises(GitCommandError) as exc_info:
        await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert "merge-base origin/main HEAD" in str(exc_info.value)
    # Raises during batch A, before the batch-B diff round-trip.
    assert client.run_commands.await_count == 1


async def test_status_snapshot_raises_when_batch_b_main_diff_fails() -> None:
    """The main working-tree diff moved to batch B; a non-zero exit there must still raise."""
    client = MagicMock()
    client.run_commands = AsyncMock(side_effect=[_resp(*_batch_a()), _resp(("fatal: bad object", 128))])
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    with pytest.raises(GitCommandError) as exc_info:
        await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert "diff mb123" in str(exc_info.value)
    assert client.run_commands.await_count == 2


async def test_status_snapshot_folds_untracked_into_batch_b() -> None:
    client = MagicMock()
    client.run_commands = AsyncMock(
        side_effect=[
            _resp(*_batch_a(status="?? new.py\n", untracked="new.py\n")),
            _resp(("", 0), ("", 0), ("+++ b/new.py\n+hello\n", 1)),  # diff <mb>, diff HEAD, diff --no-index new.py
        ]
    )
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert snap.dirty is True
    assert "new.py" in snap.diff
    assert "new.py" in snap.uncommitted_diff
    assert client.run_commands.await_count == 2
    assert "diff HEAD" in client.run_commands.await_args_list[1].args[1].commands[1]


async def test_status_snapshot_raises_when_batch_b_head_diff_fails() -> None:
    gm, _ = _sandbox_manager({"status --porcelain": (0, " M a.py\n"), "diff HEAD": (128, "fatal: bad object")})
    with pytest.raises(GitCommandError) as exc_info:
        await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert "diff HEAD" in str(exc_info.value)


async def test_status_snapshot_uncommitted_diff_leaves_out_the_branchs_earlier_commits(tmp_path: Path) -> None:
    repo, _ = _init_repo_with_origin(tmp_path)
    repo.git.switch("-c", "feature")
    _commit_file(repo, "committed.txt", "first turn\n")
    (tmp_path / "work" / "README.md").write_text("second turn\n")
    (tmp_path / "work" / "untracked.txt").write_text("second turn\n")

    snap = await GitManager(LocalGitRunner(repo)).status_snapshot(base_branch="main", mr_source_branch=None)

    assert {f.path for f in PatchSet.from_string(snap.diff)} == {"committed.txt", "README.md", "untracked.txt"}
    assert {f.path for f in PatchSet.from_string(snap.uncommitted_diff)} == {"README.md", "untracked.txt"}


async def test_status_snapshot_reuses_the_merge_base_diff_when_head_is_the_merge_base() -> None:
    head = FakeSandboxClient.HEAD_SHA
    gm, client = _sandbox_manager({
        "status --porcelain": (0, " M a.py\n"),
        "merge-base": (0, f"{head}\n"),
        f"diff {head}": (0, "+++ b/a.py\n"),
    })
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch=None)
    assert snap.uncommitted_diff == snap.diff == "+++ b/a.py\n"
    assert not client.ran("diff HEAD")


@pytest.mark.parametrize(
    "status,merge_head,expected", [("", None, ""), (" M a.py\n", "b" * 40, None)], ids=["clean tree", "pending merge"]
)
async def test_status_snapshot_skips_the_head_diff_nobody_reads(
    status: str, merge_head: str | None, expected: str | None
) -> None:
    gm, client = _sandbox_manager({"status --porcelain": (0, status), "merge-base": (0, "mb123\n")})
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch=None, merge_head=merge_head)
    assert snap.uncommitted_diff == expected
    assert not client.ran("diff HEAD")


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
    "failing_index,command_fragment",
    [(0, "status --porcelain"), (2, "ls-files --others"), (3, "ls-remote --heads"), (4, "rev-parse HEAD")],
)
async def test_status_snapshot_raises_on_batch_a_command_failure(failing_index: int, command_fragment: str) -> None:
    # A non-zero exit from any *gated* batch-A query must raise rather than parse to a misleading
    # empty value. (merge-base at index 1 is intentionally tolerant and excluded here.)
    outputs = _batch_a()
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
    client = MagicMock()
    client.run_commands = AsyncMock(side_effect=[_resp(*_batch_a(log=("abc123 commit\n", 0))), _resp(("", 0))])
    gm = GitManager(SandboxGitRunner(_backend_for(client)))
    snap = await gm.status_snapshot(base_branch="main", mr_source_branch="feat/x")
    assert snap.has_unpushed is True


async def test_status_snapshot_treats_log_failure_as_unpushed() -> None:
    # A non-zero `git log origin/<src>..HEAD` (e.g. an unknown upstream ref) is treated as "all
    # unpushed" rather than raising, mirroring has_unpushed().
    client = MagicMock()
    client.run_commands = AsyncMock(side_effect=[_resp(*_batch_a(log=("fatal: bad revision", 128))), _resp(("", 0))])
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
    assert snap.uncommitted_diff is None


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


# ---------------------------------------------------------------------------
# fast_forward
# ---------------------------------------------------------------------------


def _push_from_another_clone(tmp_path: Path, origin_dir: Path, *, name: str = "pushed.txt") -> str:
    """A commit someone else pushed to ``origin/main``; returns its sha."""
    other = Repo.clone_from(origin_dir.as_posix(), tmp_path / f"other-{name}", branch="main")
    _configure_repo_identity(other)
    (Path(other.working_dir) / name).write_text("theirs\n")
    other.git.add("-A")
    other.index.commit(f"Add {name}")
    other.remotes.origin.push("main")
    return other.head.commit.hexsha


def _publish(repo: Repo, name: str) -> None:
    (Path(repo.working_dir) / name).write_text("mine\n")
    repo.git.add("-A")
    repo.index.commit(f"Add {name}")
    repo.remotes.origin.push("main")


def _rewrite_from_another_clone(tmp_path: Path, origin_dir: Path) -> str:
    """Someone rebased ``origin/main`` onto its first commit and force-pushed it; returns the new head's sha."""
    other = Repo.clone_from(origin_dir.as_posix(), tmp_path / "rewriter", branch="main")
    _configure_repo_identity(other)
    other.git.reset("--hard", "HEAD~1")
    (Path(other.working_dir) / "rebased.txt").write_text("theirs\n")
    other.git.add("-A")
    other.index.commit("Rebased")
    other.remotes.origin.push("main", force=True)
    return other.head.commit.hexsha


async def test_fast_forward_moves_a_checkout_with_nothing_unpublished_to_a_rewritten_remote(tmp_path: Path) -> None:
    repo, origin_dir = _init_repo_with_origin(tmp_path)
    _publish(repo, "mine.txt")
    rewritten = _rewrite_from_another_clone(tmp_path, origin_dir)

    assert await GitManager(LocalGitRunner(repo)).fast_forward("main") == "replaced"
    assert repo.head.commit.hexsha == rewritten
    assert (Path(repo.working_dir) / "rebased.txt").exists()


async def test_fast_forward_keeps_unpublished_commits_when_the_remote_was_rewritten(tmp_path: Path) -> None:
    repo, origin_dir = _init_repo_with_origin(tmp_path)
    _publish(repo, "mine.txt")
    _rewrite_from_another_clone(tmp_path, origin_dir)
    (Path(repo.working_dir) / "local.txt").write_text("local\n")
    repo.git.add("-A")
    repo.index.commit("Unpublished")
    local = repo.head.commit.hexsha

    assert await GitManager(LocalGitRunner(repo)).fast_forward("main") == "diverged"
    assert repo.head.commit.hexsha == local


async def test_fast_forward_keeps_uncommitted_changes_when_the_remote_was_rewritten(tmp_path: Path) -> None:
    repo, origin_dir = _init_repo_with_origin(tmp_path)
    _publish(repo, "mine.txt")
    published = repo.head.commit.hexsha
    _rewrite_from_another_clone(tmp_path, origin_dir)
    (Path(repo.working_dir) / "mine.txt").write_text("edited\n")

    assert await GitManager(LocalGitRunner(repo)).fast_forward("main") == "diverged"
    assert repo.head.commit.hexsha == published
    assert (Path(repo.working_dir) / "mine.txt").read_text() == "edited\n"


async def test_fast_forward_advances_a_clean_checkout_to_the_remote_head(tmp_path: Path) -> None:
    repo, origin_dir = _init_repo_with_origin(tmp_path)
    pushed = _push_from_another_clone(tmp_path, origin_dir)

    assert await GitManager(LocalGitRunner(repo)).fast_forward("main") == "advanced"
    assert repo.head.commit.hexsha == pushed


async def test_fast_forward_leaves_a_checkout_with_unpublished_commits_alone(tmp_path: Path) -> None:
    repo, origin_dir = _init_repo_with_origin(tmp_path)
    _push_from_another_clone(tmp_path, origin_dir)
    (Path(repo.working_dir) / "mine.txt").write_text("mine\n")
    repo.git.add("-A")
    repo.index.commit("Unpublished")
    local = repo.head.commit.hexsha

    assert await GitManager(LocalGitRunner(repo)).fast_forward("main") == "diverged"
    assert repo.head.commit.hexsha == local


async def test_fast_forward_reports_a_remote_that_did_not_move(tmp_path: Path) -> None:
    repo, _origin_dir = _init_repo_with_origin(tmp_path)
    head = repo.head.commit.hexsha

    assert await GitManager(LocalGitRunner(repo)).fast_forward("main") == "unchanged"
    assert repo.head.commit.hexsha == head


async def test_fast_forward_works_the_same_through_a_sandbox() -> None:
    gm, client = _sandbox_manager({
        "fetch origin b": (0, ""),
        "rev-parse FETCH_HEAD": (0, "b" * 40),
        "rev-parse HEAD": (0, "a" * 40),
        "merge-base --is-ancestor HEAD FETCH_HEAD": (0, ""),
        "merge --ff-only FETCH_HEAD": (0, ""),
    })

    assert await gm.fast_forward("b") == "advanced"
    assert client.ran("merge --ff-only FETCH_HEAD")


async def test_fast_forward_through_a_sandbox_finds_an_unmoved_remote_in_three_round_trips() -> None:
    gm, client = _sandbox_manager({"rev-parse FETCH_HEAD": (0, FakeSandboxClient.HEAD_SHA)})

    assert await gm.fast_forward("b") == "unchanged"
    batches = [commands for _session, commands in client.calls_to("run_commands")]
    assert [len(batch) for batch in batches] == [4, 1, 2]
    assert "rev-parse HEAD" in batches[0][0]
    assert "fetch origin b" in batches[1][0]


_REWRITTEN_REMOTE = {
    "fetch origin b": (0, ""),
    "rev-parse FETCH_HEAD": (0, "b" * 40),
    "rev-parse HEAD": (0, "a" * 40),
    "merge-base --is-ancestor HEAD FETCH_HEAD": (1, ""),
}


async def test_fast_forward_through_a_sandbox_moves_a_published_checkout_to_a_rewritten_remote() -> None:
    gm, client = _sandbox_manager(_REWRITTEN_REMOTE)

    assert await gm.fast_forward("b") == "replaced"
    assert client.ran("reset --keep FETCH_HEAD")
    checked = next(i for i, c in enumerate(client.commands) if "--remotes=origin" in c)
    assert checked < next(i for i, c in enumerate(client.commands) if "fetch origin b" in c)


@pytest.mark.parametrize(
    "unpublished",
    [
        {"rev-list": (0, "c" * 40)},
        {"status --porcelain": (0, " M README.md\n")},
        {"rev-parse -q --verify MERGE_HEAD": (0, "d" * 40)},
    ],
    ids=["local-commit", "uncommitted-change", "pending-merge"],
)
async def test_fast_forward_through_a_sandbox_never_resets_over_unpublished_work(unpublished) -> None:
    gm, client = _sandbox_manager(unpublished | _REWRITTEN_REMOTE)

    assert await gm.fast_forward("b") == "diverged"
    assert not client.ran("reset")


async def test_fast_forward_through_a_sandbox_that_cannot_reach_the_remote_changes_nothing(caplog) -> None:
    gm, client = _sandbox_manager({"fetch origin b": (128, "fatal: unable to access")})

    with caplog.at_level("WARNING", logger="daiv.tools"):
        assert await gm.fast_forward("b") == "unchanged"

    assert not client.ran("FETCH_HEAD")
    assert "Could not fetch origin/b" in caplog.text


# ---------------------------------------------------------------------------
# Publish scope: submodules, LFS paths and recorded commits (research R8, R16, R20)
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """A ``LocalGitRunner`` that records the argument list of every command it runs."""

    def __init__(self, repo: Repo) -> None:
        self._runner = LocalGitRunner(repo)
        self.calls: list[tuple[str, ...]] = []

    async def run(self, args):
        self.calls.append(tuple(args))
        return await self._runner.run(args)

    async def run_batch(self, commands):
        self.calls.extend(tuple(c) for c in commands)
        return await self._runner.run_batch(commands)


def _submodule_app(tmp_path: Path, *, lfs_files: dict[str, bytes] | None = None):
    from tests.unit_tests.submodule_repos import LFS_ATTRIBUTES, World, git, lfs_pointer

    world = World(tmp_path)
    world.create("group/lib", {"f": "1\n"})
    files: dict[str, str] = {"README.md": "app\n"}
    if lfs_files is not None:
        files |= {".gitattributes": LFS_ATTRIBUTES, **{p: lfs_pointer(c) for p, c in lfs_files.items()}}
    world.create("group/app", files, submodules={"libs/lib": "../lib.git"})
    repo = world.clone("group/app")
    top = Path(repo.working_dir)
    git(top, "submodule", "update", "-q", "--init")
    return world, repo, top


def _committed_paths(top: Path) -> list[str]:
    from tests.unit_tests.submodule_repos import git

    return git(top, "show", "--name-only", "--format=", "HEAD").split()


async def _snapshot(gm: GitManager, scope) -> object:
    return await gm.status_snapshot(base_branch="main", mr_source_branch=None, scope=scope)


class TestDefaultScopeSendsTodaysCommands:
    async def test_snapshot_commit_fast_forward_and_push_send_exactly_todays_arguments(self, tmp_path):
        from automation.agent.workspace.git_manager import PublishScope

        _, repo, top = _submodule_app(tmp_path)
        (top / "README.md").write_text("changed\n")
        runner = _RecordingRunner(repo)
        gm = GitManager(runner)

        status = await gm.status_snapshot(base_branch="main", mr_source_branch="main", scope=PublishScope())
        assert status.kept_out.lfs_paths == () and status.kept_out.submodules == ()
        assert await gm.commit_all("msg") is True
        await gm.fast_forward("main")
        await gm.push_head_to("main", integrate_on_reject=True)

        head = runner.calls[4:5]
        assert runner.calls[:6] == [
            ("status", "--porcelain"),
            ("merge-base", "origin/main", "HEAD"),
            ("ls-files", "--others", "--exclude-standard"),
            ("ls-remote", "--heads", "origin"),
            ("rev-parse", "HEAD"),
            ("log", "origin/main..HEAD", "--oneline"),
        ]
        assert head == [("rev-parse", "HEAD")]
        assert runner.calls[6][0] == "diff" and runner.calls[6][1] != "--ignore-submodules=all"
        assert ("add", "-A") in runner.calls and ("commit", "-m", "msg") in runner.calls
        assert ("status", "--porcelain") in runner.calls[runner.calls.index(("commit", "-m", "msg")) :]
        assert not any("--ignore-submodules=all" in call or "--literal-pathspecs" in call for call in runner.calls)
        assert not any(call[:2] == ("diff", "--cached") for call in runner.calls)


class TestSubmoduleScope:
    SCOPE_KWARGS = {"submodules": True}

    def _scope(self, **kwargs):
        from automation.agent.workspace.git_manager import PublishScope

        return PublishScope(**self.SCOPE_KWARGS, **kwargs)

    async def test_an_edit_inside_a_submodule_alone_is_kept_out(self, tmp_path):
        _, repo, top = _submodule_app(tmp_path)
        (top / "libs/lib/f").write_text("edited\n")

        status = await _snapshot(GitManager(LocalGitRunner(repo)), self._scope())

        assert (status.dirty, status.diff) == (False, "")
        (sub,) = status.kept_out.submodules
        assert (sub.path, sub.changed_files, sub.commit_moved) == ("libs/lib", ("f",), False)

    async def test_only_the_superprojects_file_is_committed(self, tmp_path):
        _, repo, top = _submodule_app(tmp_path)
        (top / "README.md").write_text("changed\n")
        (top / "libs/lib/f").write_text("edited\n")
        gm = GitManager(LocalGitRunner(repo))

        status = await _snapshot(gm, self._scope())
        assert status.dirty
        assert await gm.commit_all("msg", keep_out=status.kept_out.paths, record={}) is True

        assert _committed_paths(top) == ["README.md"]

    async def test_a_moved_submodule_head_stays_out_of_the_commit(self, tmp_path):
        from tests.unit_tests.submodule_repos import git

        _, repo, top = _submodule_app(tmp_path)
        git(top / "libs/lib", "commit", "-q", "--allow-empty", "-m", "moved")
        (top / "README.md").write_text("changed\n")
        gm = GitManager(LocalGitRunner(repo))

        status = await _snapshot(gm, self._scope())
        await gm.commit_all("msg", keep_out=status.kept_out.paths, record={})

        assert status.kept_out.submodules[0].commit_moved
        assert _committed_paths(top) == ["README.md"]

    @pytest.mark.parametrize("how", ["deleted", "moved"])
    async def test_a_removed_or_moved_submodule_stays_out_of_the_commit(self, tmp_path, how):
        import shutil

        from tests.unit_tests.submodule_repos import git

        _, repo, top = _submodule_app(tmp_path)
        before = git(top, "ls-tree", "HEAD", "libs/lib")
        if how == "deleted":
            shutil.rmtree(top / "libs/lib")
        else:
            git(top, "mv", "libs/lib", "libs/moved")
        (top / "README.md").write_text("changed\n")
        gm = GitManager(LocalGitRunner(repo))

        status = await _snapshot(gm, self._scope())
        await gm.commit_all("msg", keep_out=status.kept_out.paths, record={})

        removed = {sub.path for sub in status.kept_out.submodules if sub.removed}
        assert removed == {"libs/lib"}
        assert git(top, "ls-tree", "HEAD", "libs/lib") == before
        assert git(top, "ls-tree", "HEAD", "libs/moved") == ""

    async def test_an_added_submodule_stays_out_but_gitmodules_is_committed(self, tmp_path):
        from tests.unit_tests.submodule_repos import git

        world, repo, top = _submodule_app(tmp_path)
        world.create("group/extra")
        git(top, "submodule", "add", "-q", str(world.bare("group/extra")), "libs/extra")
        gm = GitManager(LocalGitRunner(repo))

        status = await _snapshot(gm, self._scope())
        await gm.commit_all("msg", keep_out=status.kept_out.paths, record={})

        assert any(sub.path == "libs/extra" and sub.added for sub in status.kept_out.submodules)
        assert _committed_paths(top) == [".gitmodules"]
        assert git(top, "ls-tree", "HEAD", "libs/extra") == ""

    async def test_a_repository_cloned_inside_the_checkout_alone_is_no_change(self, tmp_path):
        from tests.unit_tests.submodule_repos import git

        world, repo, top = _submodule_app(tmp_path)
        world.create("group/extra")
        git(top, "clone", "-q", str(world.bare("group/extra")), "tools/vendored")
        gm = GitManager(LocalGitRunner(repo))
        head = git(top, "rev-parse", "HEAD")

        status = await _snapshot(gm, self._scope())

        assert status.dirty is False
        assert [(sub.path, sub.added) for sub in status.kept_out.submodules] == [("tools/vendored", True)]
        assert await gm.commit_all("msg", keep_out=status.kept_out.paths, record={}) is False
        assert git(top, "rev-parse", "HEAD") == head

    async def test_more_than_twenty_files_are_capped(self, tmp_path):
        _, repo, top = _submodule_app(tmp_path)
        for i in range(25):
            (top / f"libs/lib/n{i:02}.txt").write_text("x")

        status = await _snapshot(GitManager(LocalGitRunner(repo)), self._scope())

        (sub,) = status.kept_out.submodules
        assert (len(sub.changed_files), sub.more) == (20, 5)

    @pytest.mark.parametrize(("scoped", "expected"), [(False, "diverged"), (True, "replaced")])
    async def test_fast_forward_ignores_changes_kept_out_of_publishes(self, tmp_path, scoped, expected):
        from automation.agent.workspace.git_manager import PublishScope
        from tests.unit_tests.submodule_repos import git

        world, repo, top = _submodule_app(tmp_path)
        (top / "libs/lib/f").write_text("edited\n")
        work = world.work("group/app")
        git(work, "commit", "-q", "--amend", "-m", "rewritten")
        git(work, "push", "-q", "--force", "origin", "HEAD:main")
        scope = self._scope() if scoped else PublishScope()

        assert await GitManager(LocalGitRunner(repo)).fast_forward("main", scope=scope) == expected

    async def test_fast_forward_still_advances_with_changes_inside_a_submodule(self, tmp_path):
        world, repo, top = _submodule_app(tmp_path)
        (top / "libs/lib/f").write_text("edited\n")
        world.commit("group/app", {"other.txt": "x"})

        assert await GitManager(LocalGitRunner(repo)).fast_forward("main", scope=self._scope()) == "advanced"


class TestRecord:
    def _lib_commit(self, world, message: str = "lib change") -> str:
        return world.commit("group/lib", {"f": f"{message}\n"}, message=message)

    async def test_a_recorded_commit_counts_as_a_change_with_a_summary(self, tmp_path):
        from automation.agent.workspace.git_manager import PublishScope
        from tests.unit_tests.submodule_repos import git

        world, repo, top = _submodule_app(tmp_path)
        old = git(top / "libs/lib", "rev-parse", "HEAD").strip()
        new = self._lib_commit(world, "fix the lib")
        git(top / "libs/lib", "fetch", "-q", "origin")

        status = await _snapshot(
            GitManager(LocalGitRunner(repo)), PublishScope(submodules=True, record={"libs/lib": new})
        )

        assert status.dirty is True
        assert f"Submodule libs/lib {old[:7]}..{new[:7]}:\n  > fix the lib\n" in status.diff
        assert status.kept_out.submodules == ()

    async def test_a_commit_missing_from_the_clone_shows_its_short_ids(self, tmp_path):
        from automation.agent.workspace.git_manager import PublishScope
        from tests.unit_tests.submodule_repos import git

        _, repo, top = _submodule_app(tmp_path)
        old = git(top / "libs/lib", "rev-parse", "HEAD").strip()
        missing = "d" * 40

        status = await _snapshot(
            GitManager(LocalGitRunner(repo)), PublishScope(submodules=True, record={"libs/lib": missing})
        )

        assert f"Submodule libs/lib {old[:7]}...{missing[:7]} (commits not present)" in status.diff

    async def test_commit_all_records_exactly_the_given_commit(self, tmp_path):
        from tests.unit_tests.submodule_repos import git

        world, repo, top = _submodule_app(tmp_path)
        world.create("group/other")
        git(top / "libs/lib", "commit", "-q", "--allow-empty", "-m", "local, never recorded")
        unknown = "e" * 40
        gm = GitManager(LocalGitRunner(repo))

        assert await gm.commit_all("msg", keep_out=("libs/lib",), record={"libs/lib": unknown}) is True

        assert git(top, "ls-tree", "HEAD", "libs/lib").split()[:3] == ["160000", "commit", unknown]


class TestRemoteMovedARecordedCommit:
    """Research R20: a remote branch that moved a gitlink the run's commit also moves keeps the remote's commit."""

    def _setup(self, tmp_path):
        from tests.unit_tests.submodule_repos import git

        world, repo, top = _submodule_app(tmp_path)
        lib = world.work("group/lib")
        base = world.head("group/lib")
        git(lib, "checkout", "-q", "-b", "x", base)
        remote_commit = world.commit("group/lib", {"x": "x"}, branch="x")
        git(lib, "checkout", "-q", "-b", "y", base)
        run_commit = world.commit("group/lib", {"y": "y"}, branch="y")
        app = world.work("group/app")
        git(app / "libs/lib", "fetch", "-q", "origin")
        git(app / "libs/lib", "checkout", "-q", remote_commit)
        git(app, "commit", "-qam", "someone moved the lib")
        git(app, "push", "-q", "origin", "HEAD:main")
        git(top / "libs/lib", "fetch", "-q", "origin")
        return world, repo, top, remote_commit, run_commit

    async def _publish(self, repo, run_commit, *, readme: bool = True):
        from automation.agent.workspace.git_manager import PublishScope

        top = Path(repo.working_dir)
        if readme:
            (top / "README.md").write_text("run change\n")
        gm = GitManager(LocalGitRunner(repo))
        record = {"libs/lib": run_commit}
        await gm.commit_all("run", keep_out=(), record=record)
        await gm.push_head_to("main", integrate_on_reject=True, scope=PublishScope(submodules=True, record=record))
        return gm

    async def test_a_rebase_keeps_the_remotes_commit_and_the_runs_other_changes(self, tmp_path):
        from tests.unit_tests.submodule_repos import git

        world, repo, _, remote_commit, run_commit = self._setup(tmp_path)

        gm = await self._publish(repo, run_commit)

        assert gm.remote_moved == ("libs/lib",)
        work = world.work("group/app")
        git(work, "fetch", "-q", "origin")
        assert git(work, "ls-tree", "origin/main", "libs/lib").split()[2] == remote_commit
        assert git(work, "show", "origin/main:README.md") == "run change\n"

    async def test_a_merge_does_the_same(self, tmp_path):
        from tests.unit_tests.submodule_repos import git

        world, repo, top, remote_commit, run_commit = self._setup(tmp_path)
        git(top, "checkout", "-q", "-b", "side")
        (top / "side.txt").write_text("side\n")
        git(top, "add", "side.txt")
        git(top, "commit", "-qm", "side")
        git(top, "checkout", "-q", "main")
        git(top, "merge", "-q", "--no-ff", "--no-edit", "side")

        gm = await self._publish(repo, run_commit)

        assert gm.remote_moved == ("libs/lib",)
        work = world.work("group/app")
        git(work, "fetch", "-q", "origin")
        assert git(work, "ls-tree", "origin/main", "libs/lib").split()[2] == remote_commit
        assert git(work, "show", "origin/main:side.txt") == "side\n"

    async def test_a_conflict_on_an_ordinary_file_still_aborts(self, tmp_path):
        world, repo, top, _, run_commit = self._setup(tmp_path)
        world.commit("group/app", {"README.md": "theirs\n"})

        with pytest.raises(GitPushStaleError):
            await self._publish(repo, run_commit)

    async def test_a_commit_the_resolution_empties_is_dropped(self, tmp_path):
        from tests.unit_tests.submodule_repos import git

        world, repo, top, remote_commit, run_commit = self._setup(tmp_path)

        gm = await self._publish(repo, run_commit, readme=False)

        assert gm.remote_moved == ("libs/lib",)
        work = world.work("group/app")
        git(work, "fetch", "-q", "origin")
        assert git(work, "log", "--format=%s", "-1", "origin/main").strip() == "someone moved the lib"


class TestLfsScope:
    def _scope(self):
        from automation.agent.workspace.git_manager import PublishScope

        return PublishScope(lfs=True)

    async def test_lfs_paths_are_kept_out_and_listed(self, tmp_path):
        from tests.unit_tests.submodule_repos import git

        _, repo, top = _submodule_app(tmp_path, lfs_files={"a.bin": b"a", "b.bin": b"b", "c.bin": b"c"})
        (top / "a.bin").write_bytes(b"edited")
        (top / "new.bin").write_bytes(b"new")
        (top / "b.bin").unlink()
        git(top, "mv", "c.bin", "d.bin")
        (top / "README.md").write_text("changed\n")
        runner = _RecordingRunner(repo)
        gm = GitManager(runner)

        status = await _snapshot(gm, self._scope())
        await gm.commit_all("msg", keep_out=status.kept_out.paths)

        assert set(status.kept_out.lfs_paths) == {"a.bin", "new.bin", "b.bin", "c.bin", "d.bin"}
        assert _committed_paths(top) == ["README.md"]
        for args in (("status", "--porcelain"), ("ls-files", "--others", "--exclude-standard")):
            assert (*args, "--", ".", ":(exclude,attr:filter=lfs)") in runner.calls
        assert any(call[0] == "diff" and call[-2:] == (".", ":(exclude,attr:filter=lfs)") for call in runner.calls)

    async def test_lfs_changes_alone_are_no_change(self, tmp_path):
        _, repo, top = _submodule_app(tmp_path, lfs_files={"a.bin": b"a"})
        (top / "a.bin").write_bytes(b"edited")

        status = await _snapshot(GitManager(LocalGitRunner(repo)), self._scope())

        assert (status.dirty, status.kept_out.lfs_paths) == (False, ("a.bin",))

    @pytest.mark.parametrize(("scoped", "expected"), [(False, "diverged"), (True, "replaced")])
    async def test_fast_forward_ignores_lfs_changes(self, tmp_path, scoped, expected):
        from automation.agent.workspace.git_manager import PublishScope
        from tests.unit_tests.submodule_repos import git

        world, repo, top = _submodule_app(tmp_path, lfs_files={"a.bin": b"a"})
        (top / "a.bin").write_bytes(b"edited")
        work = world.work("group/app")
        git(work, "commit", "-q", "--amend", "-m", "rewritten")
        git(work, "push", "-q", "--force", "origin", "HEAD:main")
        scope = self._scope() if scoped else PublishScope()

        assert await GitManager(LocalGitRunner(repo)).fast_forward("main", scope=scope) == expected

    async def test_fast_forward_still_advances_with_an_lfs_change(self, tmp_path):
        world, repo, top = _submodule_app(tmp_path, lfs_files={"a.bin": b"a"})
        (top / "a.bin").write_bytes(b"edited")
        world.commit("group/app", {"other.txt": "x"})

        assert await GitManager(LocalGitRunner(repo)).fast_forward("main", scope=self._scope()) == "advanced"


async def test_a_push_from_a_detached_head_creates_the_branch(tmp_path):
    repo, origin_dir = _init_repo_with_origin(tmp_path)
    repo.git.checkout("--detach")
    (Path(repo.working_dir) / "new.txt").write_text("x\n")
    gm = GitManager(LocalGitRunner(repo))
    await gm.commit_all("detached work")

    with pytest.raises(GitCommandError):
        await gm.push_head_to("daiv/fresh")
    assert await gm.push_head_to("daiv/fresh", from_detached=True) == "daiv/fresh"

    assert Repo(origin_dir).git.rev_parse("refs/heads/daiv/fresh") == repo.head.commit.hexsha
