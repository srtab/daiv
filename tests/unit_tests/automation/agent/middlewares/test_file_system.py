from __future__ import annotations

import stat
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware as UpstreamFilesystemMiddleware
from deepagents.middleware.filesystem import _check_fs_permission
from langchain_core.messages import ToolMessage

from automation.agent.middlewares import file_system as fs_module
from automation.agent.middlewares.file_system import (
    EDIT_SUCCESS_PREFIX,
    WORKSPACE_FENCE_PERMISSIONS,
    WRITE_SUCCESS_PREFIX,
    DAIVFilesystemBackend,
    build_disk_workspace_backend,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def working_repo(tmp_path) -> Path:
    """Test layout: tmp_path/myrepo/."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    return repo


@pytest.fixture
def setup(working_repo):
    """Disk-backed repo backend + the upstream filesystem tool map."""
    backend = DAIVFilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    fs = UpstreamFilesystemMiddleware(backend=backend, custom_tool_descriptions=fs_module.CUSTOM_TOOL_DESCRIPTIONS)
    tools = {tool.name: tool for tool in fs.tools}
    return SimpleNamespace(backend=backend, tools=tools, repo=working_repo)


def _runtime(*, state: dict[str, Any], working_dir: Path) -> SimpleNamespace:
    """A minimal ``ToolRuntime`` shim sufficient for the upstream filesystem tools."""
    return SimpleNamespace(
        state=state,
        context=SimpleNamespace(gitrepo=SimpleNamespace(working_dir=str(working_dir)), has_repo=True),
        tool_call_id="call_test",
    )


# ---------------------------------------------------------------------------
# Upstream contract pinning
# ---------------------------------------------------------------------------


async def test_upstream_success_prefixes_remain_stable(setup):
    """Pin the upstream write/edit success-message prefixes.

    ``WRITE_SUCCESS_PREFIX``/``EDIT_SUCCESS_PREFIX`` mirror deepagents' filesystem-tool
    success strings; pinning them makes a deepagents reword fail this test instead of
    silently drifting from the constants DAIV keeps in lockstep with upstream.
    """
    runtime = _runtime(state={}, working_dir=setup.repo)

    write_result = await setup.tools["write_file"].coroutine(
        file_path=f"/{setup.repo.name}/contract.py", content="x", runtime=runtime
    )
    write_text = write_result.content if isinstance(write_result, ToolMessage) else write_result
    assert write_text.startswith(WRITE_SUCCESS_PREFIX), (
        f"upstream changed write success format; update WRITE_SUCCESS_PREFIX: {write_text!r}"
    )

    edit_result = await setup.tools["edit_file"].coroutine(
        file_path=f"/{setup.repo.name}/contract.py", old_string="x", new_string="y", runtime=runtime
    )
    edit_text = edit_result.content if isinstance(edit_result, ToolMessage) else edit_result
    assert edit_text.startswith(EDIT_SUCCESS_PREFIX), (
        f"upstream changed edit success format; update EDIT_SUCCESS_PREFIX: {edit_text!r}"
    )


def test_write_file_description_does_not_contradict_upstream():
    """DAIV's write_file guidance must not assert a mechanism upstream has changed.

    Through deepagents 0.6 the disk backend refused to overwrite, so DAIV's appended text said
    write_file "can ONLY create new files". 0.7 replaces the file instead and says so in its own
    description, which turned the appended line into a contradiction inside one tool description.
    Whether an overwrite is refused is now backend-dependent, so the guidance may only express a
    preference.
    """
    from automation.agent.middlewares.file_system import WRITE_FILE_TOOL_DESCRIPTION

    assert "edit_file" in WRITE_FILE_TOOL_DESCRIPTION, "must still steer edits to edit_file"
    assert "ONLY create new files" not in WRITE_FILE_TOOL_DESCRIPTION
    assert "will fail on files that already exist" not in WRITE_FILE_TOOL_DESCRIPTION


async def test_glob_description_does_not_claim_bare_patterns_fail(tmp_path: Path):
    """A bare repo-relative glob pattern does match, so the description must not say otherwise.

    ``CompositeBackend.aglob`` merges the default backend with every route when ``path`` is unset,
    so ``tests/**/*.py`` resolves under ``/workspace/repo`` without a ``**/`` prefix. Pin both the
    claim and the behaviour, so neither can drift back independently.
    """
    from automation.agent.constants import REPO_PATH
    from automation.agent.middlewares.file_system import GLOB_TOOL_DESCRIPTION, build_disk_workspace_backend

    assert "matches nothing under the repo" not in GLOB_TOOL_DESCRIPTION
    assert "repository root" in GLOB_TOOL_DESCRIPTION

    clone = tmp_path / "repo"
    (clone / "tests").mkdir(parents=True)
    (clone / "tests" / "test_a.py").write_text("x\n")
    backend = build_disk_workspace_backend(clone)

    bare = await backend.aglob("tests/**/*.py")
    paths = [e["path"] for e in (bare.matches or [])]

    assert f"{REPO_PATH}/tests/test_a.py" in paths, "a bare repo-relative pattern must still match"
    # Scoping to the repo root is what yields the single canonical path the description recommends.
    scoped = await backend.aglob("tests/**/*.py", path=REPO_PATH)
    assert [e["path"] for e in (scoped.matches or [])] == [f"{REPO_PATH}/tests/test_a.py"]


def test_grep_arg_schema_describes_regex(setup):
    """The grep tool's input schema must describe ``pattern`` as a regex, not literal text.

    deepagents ships ``GrepSchema`` with a "literal string, not regex" ``pattern`` description (and a
    "current working directory" ``path`` default) that the model sees in the tool's input schema
    alongside DAIV's regex-aware tool description — a direct contradiction. ``_align_arg_schema``
    rewrites both at import; pin them here so a deepagents bump that reworks GrepSchema fails loudly
    instead of silently restoring the contradiction.
    """
    props = setup.tools["grep"].args_schema.model_json_schema()["properties"]

    pattern_desc = props["pattern"]["description"]
    assert "regular expression" in pattern_desc.lower()
    assert "literal" not in pattern_desc.lower()
    assert pattern_desc == fs_module._GREP_PATTERN_ARG_DESCRIPTION
    assert props["path"]["description"] == fs_module._GREP_PATH_ARG_DESCRIPTION


def test_glob_description_steers_over_find(setup):
    """glob's own description must steer the model away from shell `find` and warn about the
    `/`-anchoring footgun, mirroring grep's 'prefer over bash' treatment."""
    desc = setup.tools["glob"].description
    low = desc.lower()
    assert "prefer this tool" in low
    assert "shell `find`" in low  # names the shell tool it replaces
    assert "**/" in desc  # the anchoring guidance the model must learn
    assert fs_module._GLOB_EXTRA in desc


def test_glob_arg_schema_warns_root_anchoring(setup):
    """deepagents ships GlobSchema with a bare `*.txt` pattern example and a "Defaults to root '/'"
    path description. `_align_arg_schema` rewrites both at import; pin them so a deepagents bump
    that reworks GlobSchema fails loudly instead of silently regressing."""
    props = setup.tools["glob"].args_schema.model_json_schema()["properties"]
    assert props["pattern"]["description"] == fs_module._GLOB_PATTERN_ARG_DESCRIPTION
    assert props["path"]["description"] == fs_module._GLOB_PATH_ARG_DESCRIPTION
    # the bare `*.txt` example (no `**/` prefix) must be replaced
    assert "*.txt" not in props["pattern"]["description"]
    # the path description must warn it is NOT the repository root
    assert "filesystem root" in props["path"]["description"].lower()


def test_ls_description_steers_over_shell_ls(setup):
    """ls's own description must steer the model away from shell `ls`, reframe it as a directory
    explorer (not only a read precursor), and state that `path` is required/absolute (shell `ls`
    defaults to cwd; the dedicated tool errors with no path)."""
    desc = setup.tools["ls"].description
    low = desc.lower()
    assert "prefer this tool" in low
    assert "shell `ls`" in low
    assert "required" in low  # the no-implicit-cwd footgun
    assert fs_module._LS_EXTRA in desc


class TestDiskBackendRegexGrep:
    def _backend(self, tmp_path: Path):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        (tmp_path / "a.py").write_text("alpha line\ngamma line\n")
        (tmp_path / "b.py").write_text("beta line\n")
        return DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)

    async def test_disk_grep_alternation_matches(self, tmp_path: Path):
        backend = self._backend(tmp_path)
        result = await backend.agrep("alpha|beta")
        assert result.error is None
        texts = sorted(m["text"] for m in (result.matches or []))
        assert texts == ["alpha line", "beta line"]

    async def test_disk_grep_invalid_regex_is_clean_error(self, tmp_path: Path):
        backend = self._backend(tmp_path)
        result = await backend.agrep("alpha(")
        assert result.error is not None
        assert "invalid regular expression" in result.error
        assert not result.matches

    async def test_disk_grep_max_count_caps_and_flags_truncation(self, tmp_path: Path):
        backend = self._backend(tmp_path)
        result = await backend.agrep("line", max_count=1)
        assert result.error is None
        assert len(result.matches or []) == 1
        assert result.truncated is True

    async def test_disk_grep_without_max_count_is_not_truncated(self, tmp_path: Path):
        backend = self._backend(tmp_path)
        result = await backend.agrep("line")
        assert len(result.matches or []) == 3
        assert result.truncated is False

    async def test_disk_grep_glob_filters_by_extension(self, tmp_path: Path):
        (tmp_path / "keep.py").write_text("needle here\n")
        (tmp_path / "skip.txt").write_text("needle here\n")
        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)

        result = await backend.agrep("needle", glob="*.py")

        assert result.error is None
        paths = sorted(m["path"] for m in (result.matches or []))
        assert paths == ["/keep.py"]

    @staticmethod
    def _raise_on_locked(monkeypatch):
        """Make opening ``locked.py`` raise a realistic ``PermissionError`` (whose ``str`` embeds
        the absolute path), leaving every other file readable."""
        import pathlib

        real_open = pathlib.Path.open

        def fake_open(self, *args, **kwargs):
            if self.name == "locked.py":
                raise PermissionError(13, "Permission denied", str(self))
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "open", fake_open)

    async def test_disk_grep_unreadable_file_with_matches_returns_clean_and_logs(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """A non-actionable per-file read error must not withhold usable matches: with a good match
        present the result comes back clean (so the composite still merges the other backends) and
        the failure goes to the logs -- never embedding the real on-disk path (``virtual_mode``)."""
        import logging

        (tmp_path / "good.py").write_text("needle here\n")
        (tmp_path / "locked.py").write_text("needle here\n")
        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        self._raise_on_locked(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="daiv.tools"):
            result = await backend.agrep("needle")

        # Usable matches survive and the result is NOT flagged as an error.
        assert result.error is None
        assert any(m["path"] == "/good.py" for m in (result.matches or []))
        # The failure is logged for operators, sanitized (no real host path).
        assert "could not fully search" in caplog.text
        assert "PermissionError" in caplog.text and "Permission denied" in caplog.text
        assert str(tmp_path) not in caplog.text

    async def test_disk_grep_all_unreadable_surfaces_sanitized_error(self, tmp_path: Path, monkeypatch):
        """With no matches to salvage the failure IS surfaced (so an empty result is not mistaken
        for a genuine zero-match) -- still sanitized to keep the real host path out."""
        (tmp_path / "locked.py").write_text("needle here\n")
        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        self._raise_on_locked(monkeypatch)

        result = await backend.agrep("needle")

        assert not result.matches
        assert result.error is not None
        assert "could not be fully searched" in result.error
        assert "PermissionError" in result.error and "Permission denied" in result.error
        assert str(tmp_path) not in result.error

    async def test_disk_grep_timeout_is_flagged(self, tmp_path: Path, monkeypatch):
        backend = self._backend(tmp_path)
        # Force the wall-clock deadline into the past so the first walk step trips it.
        monkeypatch.setattr(fs_module, "DEFAULT_GREP_TIMEOUT", -1)

        result = await backend.agrep("alpha")

        assert result.error is not None
        assert "timed out" in result.error

    async def test_disk_grep_globstar_matches_nested_but_star_does_not(self, tmp_path: Path):
        """Pins the anchoring footgun the tool description warns about: ``**/*.py`` (GLOBSTAR)
        reaches into subdirectories, a bare ``*.py`` does not — matched against the path relative
        to root, not the bare filename."""
        (tmp_path / "top.py").write_text("needle\n")
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "nested.py").write_text("needle\n")
        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)

        globstar = await backend.agrep("needle", glob="**/*.py")
        star = await backend.agrep("needle", glob="*.py")

        globstar_paths = {m["path"] for m in (globstar.matches or [])}
        star_paths = {m["path"] for m in (star.matches or [])}
        assert "/pkg/nested.py" in globstar_paths  # GLOBSTAR crosses directories
        assert "/pkg/nested.py" not in star_paths  # a bare * does not
        assert "/top.py" in star_paths

    async def test_disk_grep_binary_file_is_skipped_silently(self, tmp_path: Path, caplog):
        """A file that fails to decode on its first read is treated as binary: skipped with no
        ``error`` and no warning, while sibling matches are returned (a regression that dropped the
        ``scanned_lines`` guard would flag every binary and, in an all-binary dir, spuriously fail)."""
        import logging

        (tmp_path / "good.py").write_text("needle here\n")
        (tmp_path / "image.bin").write_bytes(b"\xff\xfe\x00 not valid utf-8 \x80\x81")
        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)

        with caplog.at_level(logging.WARNING, logger="daiv.tools"):
            result = await backend.agrep("needle")

        assert result.error is None
        assert any(m["path"] == "/good.py" for m in (result.matches or []))
        assert "could not fully search" not in caplog.text  # binary skip is silent, not flagged

    async def test_disk_grep_partial_decode_is_flagged(self, tmp_path: Path, caplog):
        """A file whose valid prefix exceeds the text read-buffer before hitting invalid bytes is a
        *mid-file* decode failure (``scanned_lines > 0``): its matches are kept and the truncation is
        recorded, unlike a first-chunk binary skip."""
        import logging

        # ~35 KB of valid matching lines (well past TextIOWrapper's ~8 KB read chunk) then bad bytes,
        # so decoding only fails after many lines have already been scanned and matched.
        (tmp_path / "truncated.py").write_bytes(b"needle\n" * 5000 + b"\xff\xfe still not utf-8\n")
        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)

        with caplog.at_level(logging.WARNING, logger="daiv.tools"):
            result = await backend.agrep("needle")

        # Matches from the decodable prefix survive; the partial read is flagged (returned clean
        # because matches exist, and logged for operators).
        assert result.error is None
        assert result.matches and any(m["path"] == "/truncated.py" for m in result.matches)
        assert "could not fully search" in caplog.text
        assert "UnicodeDecodeError" in caplog.text

    def test_grep_error_detail_sanitizes_and_respects_virtual_mode(self, tmp_path: Path):
        """Direct contract for the sanitizer: never leak the on-disk path, and only include generic
        exception text (which can carry ``root_dir``) when NOT in virtual mode."""
        virt = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        disk = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=False)

        # OSError: only strerror, never the filename ``str(exc)`` would append.
        oserr = PermissionError(13, "Permission denied", str(tmp_path / "secret"))
        assert virt._grep_error_detail(oserr) == "PermissionError: Permission denied"
        assert str(tmp_path) not in virt._grep_error_detail(oserr)

        # UnicodeDecodeError: its path-free ``reason``.
        ude = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        assert virt._grep_error_detail(ude) == "UnicodeDecodeError: invalid start byte"

        # Generic exception: message suppressed under virtual_mode (it may carry the real root),
        # included otherwise.
        generic = RuntimeError(f"boom at {tmp_path}")
        assert virt._grep_error_detail(generic) == "RuntimeError"
        assert disk._grep_error_detail(generic) == f"RuntimeError: boom at {tmp_path}"


class TestDAIVCompositeBackend:
    """Composite routing must preserve the prefix-stripping invariant for DAIV's two
    extension methods (``unlink``/``stat_mode``) and the dispatch helper
    (``resolve_backend_for``), so a routed op reaches the right underlying backend with
    the route prefix stripped.
    """

    @staticmethod
    def _make_composite(tmp_path: Path):
        """Compose a repo + skills backend pair the same way the disk-backed path does.

        ``DAIVFilesystemBackend(virtual_mode=True)`` interprets virtual paths as
        ``/<root_dir.name>/<rel>`` and resolves them to ``root_dir.parent/<root_dir.name>/<rel>``,
        so each backend's ``root_dir`` must literally exist on disk. We layer ``repo-mount``
        and ``skills-mount`` under ``tmp_path`` and address them via ``/repo-mount/...`` and
        ``/skills/...`` respectively.

        The skills route uses an unrelated virtual prefix on purpose: the route strips
        ``/skills/`` before delegating to a backend whose virtual root is ``/skills-mount``,
        and ``_resolve_path("foo.md")`` happens to land under ``skills-mount`` because
        ``virtual_mode`` falls back to root-relative for paths without the agent prefix.
        """
        from automation.agent.middlewares.file_system import DAIVCompositeBackend

        skills_root = tmp_path / "skills-mount"
        skills_root.mkdir()
        repo_root = tmp_path / "repo-mount"
        repo_root.mkdir()
        skills = DAIVFilesystemBackend(root_dir=skills_root, virtual_mode=True)
        repo = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        composite = DAIVCompositeBackend(default=repo, routes={"/skills/": skills})
        return composite, skills, repo, skills_root, repo_root

    async def test_unlink_strips_prefix_for_routed_path(self, tmp_path: Path):
        composite, _skills, _repo, skills_root, _repo_root = self._make_composite(tmp_path)
        (skills_root / "foo.md").write_text("x")

        ok = await composite.unlink("/skills/foo.md")

        assert ok is True
        assert not (skills_root / "foo.md").exists()

    async def test_unlink_passes_default_path_unchanged(self, tmp_path: Path):
        composite, _skills, _repo, _skills_root, repo_root = self._make_composite(tmp_path)
        target = repo_root / "bar.py"
        target.write_text("x")

        ok = await composite.unlink("/repo-mount/bar.py")

        assert ok is True
        assert not target.exists(), "default-route unlink must operate on the unstripped path"

    async def test_stat_mode_routes_to_underlying_backend(self, tmp_path: Path):
        composite, _skills, _repo, skills_root, repo_root = self._make_composite(tmp_path)
        skills_target = skills_root / "exec.sh"
        skills_target.write_text("#!/bin/sh\n")
        skills_target.chmod(0o755)
        repo_target = repo_root / "plain.py"
        repo_target.write_text("x")
        repo_target.chmod(0o644)

        skills_mode = await composite.stat_mode("/skills/exec.sh")
        repo_mode = await composite.stat_mode("/repo-mount/plain.py")

        assert stat.S_IMODE(skills_mode) == 0o755, "skills-routed stat_mode reads real bits"
        assert stat.S_IMODE(repo_mode) == 0o644, "default-routed stat_mode reads real bits"

    async def test_agrep_alternation_matches_via_composite(self, tmp_path: Path):
        """With regex grep, a `foo|bar` alternation returns real matches across routed backends —
        no literal-semantics hint, no false 'symbol does not exist'.

        ``_make_composite`` layers ``skills-mount`` *under* the default backend's root (``tmp_path``),
        so a file in the skills route is also visible to the default backend and surfaces from both
        (production roots are siblings and don't overlap); dedupe the texts so the assertion pins the
        alternation behavior, not the fixture's incidental double-count.
        """
        composite, _skills, _repo, skills_root, _repo_root = self._make_composite(tmp_path)
        (skills_root / "doc.md").write_text("has foo here\nand bar there\n")

        result = await composite.agrep("foo|bar")

        assert result.error is None
        texts = sorted({m["text"] for m in (result.matches or [])})
        assert texts == ["and bar there", "has foo here"]

    async def test_resolve_backend_for_returns_route_target(self, tmp_path: Path):
        composite, skills, repo, _skills_root, _repo_root = self._make_composite(tmp_path)

        assert composite.resolve_backend_for("/skills/foo") is skills
        assert composite.resolve_backend_for("/skills/") is skills
        assert composite.resolve_backend_for("/repo-mount/bar") is repo
        assert composite.resolve_backend_for("/random/baz") is repo, "unrouted falls through to default"

    async def test_constructor_rejects_backend_missing_daiv_methods(self, tmp_path: Path):
        from automation.agent.middlewares.file_system import DAIVCompositeBackend

        good = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        plain_state = SimpleNamespace()  # missing unlink + stat_mode

        with pytest.raises(TypeError, match="DAIVBackendProtocol"):
            DAIVCompositeBackend(default=good, routes={"/x/": plain_state})  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="DAIVBackendProtocol"):
            DAIVCompositeBackend(default=plain_state, routes={})  # type: ignore[arg-type]


def test_bind_session_sets_session_with_construction_client():
    from automation.agent.middlewares.file_system import SandboxFileBackend

    backend = SandboxFileBackend(client=object())
    backend.bind_session("sess-1")
    assert backend._session_id == "sess-1"


def test_bind_session_rejects_cross_session_rebind():
    from automation.agent.middlewares.file_system import SandboxFileBackend

    backend = SandboxFileBackend(client=object(), session_id="sess-1")
    with pytest.raises(RuntimeError, match="refusing"):
        backend.bind_session("sess-2")


def test_is_bound_requires_both_client_and_session():
    # is_bound is the non-raising counterpart of _require_bound and drives GitMiddleware's
    # slash-command short-circuit. BOTH conditions must hold (client AND session): an `or` slip,
    # or checking only the session, would pass the git tests (which always supply a client) yet
    # wrongly report a client-less backend as bound — so guard the two-condition logic directly.
    from automation.agent.middlewares.file_system import SandboxFileBackend

    assert SandboxFileBackend(client=None).is_bound() is False
    assert SandboxFileBackend(client=object()).is_bound() is False  # client, no session
    assert SandboxFileBackend(client=object(), session_id="sess-1").is_bound() is True


def test_filesystem_absolute_path_directive_names_the_repo_root():
    """The directive names where repo files live so the model uses the full path instead of a
    repo-relative slip (e.g. /daiv/...), without claiming that root is the only valid location."""
    from automation.agent.middlewares.file_system import filesystem_absolute_path_directive

    directive = filesystem_absolute_path_directive("/workspace/repo/")
    assert "/workspace/repo/" in directive
    assert "/workspace/repo/path/to/file.py" in directive  # full-path example
    # Must NOT forbid the scratchpad / skills the sandbox prompt tells the model it may use.
    assert "rejected" not in directive
    assert "only" not in directive.lower()


def test_filesystem_absolute_path_directive_normalizes_trailing_slash():
    """Defensive: a caller passing the root without a trailing slash still yields a single one."""
    from automation.agent.middlewares.file_system import filesystem_absolute_path_directive

    assert "/workspace/repo/path/to/file.py" in filesystem_absolute_path_directive("/workspace/repo")


class TestWorkspaceFencePermissions:
    """Disk-mode fence: allow read+write under the three real subtrees, read-only access to the
    offloaded-artifact dirs (so eviction read-back works), and deny the bare /workspace root plus
    any other path beneath it."""

    def test_allows_real_subtrees(self):
        for op in ("read", "write"):
            for path in (
                "/workspace/repo",
                "/workspace/repo/daiv/foo.py",
                "/workspace/skills",
                "/workspace/skills/code-review/SKILL.md",
                "/workspace/tmp",
                "/workspace/tmp/scratch.txt",
            ):
                assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, op, path) == "allow", (op, path)

    def test_denies_bare_root_and_unrelated_workspace_paths(self):
        # Bare /workspace (the deny needs the literal pattern; /workspace/** does not match it) and
        # any path under /workspace that isn't a real subtree or an artifact dir are denied both ways.
        for op in ("read", "write"):
            for path in ("/workspace", "/workspace/random", "/workspace/repofoo", "/workspace/repofoo/x"):
                assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, op, path) == "deny", (op, path)

    def test_artifact_dirs_are_readable_but_not_writable(self):
        # Eviction / output_to_file write here through the backend directly (bypassing the fence);
        # the agent must be able to read the offloaded file back, but never writes here itself.
        for path in (
            "/workspace/large_tool_results",
            "/workspace/large_tool_results/call_abc",
            "/workspace/conversation_history",
            "/workspace/conversation_history/uuid.md",
        ):
            assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, "read", path) == "allow", path
            assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, "write", path) == "deny", path

    def test_fence_allows_framework_offload_prefixes(self, tmp_path):
        """Drift-guard: the artifact read carve-out must cover whatever offload prefixes deepagents
        derives from ``artifacts_root``. If a framework bump renames those dirs, this fails loudly
        instead of silently re-breaking offload read-back in disk mode."""
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        backend = build_disk_workspace_backend(clone_dir, skills_cache=tmp_path / "skills_cache")
        middleware = UpstreamFilesystemMiddleware(backend=backend, _permissions=WORKSPACE_FENCE_PERMISSIONS)

        for prefix in (middleware._large_tool_results_prefix, middleware._conversation_history_prefix):
            assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, "read", f"{prefix}/some-id") == "allow", prefix
            assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, "write", f"{prefix}/some-id") == "deny", prefix

    def test_paths_outside_workspace_default_allow(self):
        assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, "read", "/etc/passwd") == "allow"


class TestBuildDiskWorkspaceBackend:
    """The disk composite maps the unified /workspace namespace onto local disk locations."""

    async def test_routes_repo_skills_and_tmp(self, tmp_path):
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        skills_cache = tmp_path / "skills_cache"
        skills_cache.mkdir()

        backend = build_disk_workspace_backend(clone_dir, skills_cache=skills_cache)

        assert (await backend.awrite("/workspace/repo/a.txt", "R")).error is None
        assert (clone_dir / "a.txt").read_text() == "R"

        assert (await backend.awrite("/workspace/skills/s.txt", "S")).error is None
        assert (skills_cache / "s.txt").read_text() == "S"

        assert (await backend.awrite("/workspace/tmp/t.txt", "T")).error is None
        assert (clone_dir.parent / "workspace" / "tmp" / "t.txt").read_text() == "T"

    def test_artifacts_root_is_workspace(self, tmp_path):
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        backend = build_disk_workspace_backend(clone_dir, skills_cache=tmp_path / "skills_cache")
        assert backend.artifacts_root == "/workspace"


class TestSandboxGrepTruncation:
    def _bound_backend(self, fs_grep_response):
        from unittest.mock import AsyncMock

        from automation.agent.middlewares.file_system import SandboxFileBackend

        client = AsyncMock()
        client.fs_grep = AsyncMock(return_value=fs_grep_response)
        backend = SandboxFileBackend(client=client, session_id="sess-1")
        return backend

    async def test_truncated_response_sets_the_flag_without_a_synthetic_match(self):
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsGrepMatch, FsGrepResponse

        resp = FsGrepResponse(
            matches=[FsGrepMatch(path=f"{REPO_PATH}/f{i}.py", line=1, text="x") for i in range(3)], truncated=True
        )
        backend = self._bound_backend(resp)

        result = await backend.agrep("x", path=REPO_PATH)

        assert result.error is None
        # deepagents 0.7 renders its own truncation guidance from `truncated`, so the note is no
        # longer smuggled through a synthetic match's `path` to survive `files_with_matches`.
        assert result.truncated is True
        assert len(result.matches) == 3, "every returned match must be a real file"
        assert all(m["path"].startswith(REPO_PATH) for m in result.matches)

    async def test_untruncated_response_has_no_note(self):
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsGrepMatch, FsGrepResponse

        resp = FsGrepResponse(matches=[FsGrepMatch(path=f"{REPO_PATH}/a.py", line=1, text="x")], truncated=False)
        backend = self._bound_backend(resp)

        result = await backend.agrep("x", path=REPO_PATH)

        assert len(result.matches) == 1
        assert result.truncated is False

    async def test_max_count_trims_and_flags_truncation(self):
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsGrepMatch, FsGrepResponse

        resp = FsGrepResponse(
            matches=[FsGrepMatch(path=f"{REPO_PATH}/f{i}.py", line=1, text="x") for i in range(5)], truncated=False
        )
        backend = self._bound_backend(resp)

        result = await backend.agrep("x", path=REPO_PATH, max_count=2)

        assert len(result.matches) == 2
        assert result.truncated is True, "trimming on this side must still be reported as truncation"
        assert all(not m["path"].startswith("(grep results truncated") for m in result.matches)

    async def test_invalid_pattern_error_maps_to_model_hint(self):
        """The sandbox returns `invalid_pattern`; the backend must rewrite it to the actionable hint
        (this is the production path — daiv doesn't validate the regex itself for the sandbox)."""
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsError, FsErrorCode, FsGrepResponse

        resp = FsGrepResponse(error=FsError(code=FsErrorCode.INVALID_PATTERN, message="invalid regular expression"))
        backend = self._bound_backend(resp)

        result = await backend.agrep("foo(", path=REPO_PATH)

        assert not result.matches
        assert result.error is not None
        assert result.error.startswith("Grep 'foo(': ")
        assert "not a valid regular expression" in result.error
        assert "escape regex metacharacters" in result.error


class TestSandboxReadPagination:
    """`SandboxFileBackend.aread` must populate deepagents' read-window fields so the middleware
    emits its truncation notice on sandbox reads (see `_sandbox_read_window`)."""

    def _bound_backend(self, fs_read_response):
        from unittest.mock import AsyncMock

        from automation.agent.middlewares.file_system import SandboxFileBackend

        client = AsyncMock()
        client.fs_read = AsyncMock(return_value=fs_read_response)
        return SandboxFileBackend(client=client, session_id="sess-1")

    async def test_full_window_advertises_more_lines(self):
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        content = "".join(f"line {i}\n" for i in range(1, 101))  # exactly `limit` lines
        backend = self._bound_backend(FsReadResponse(content=content, encoding="utf-8"))

        result = await backend.aread(f"{REPO_PATH}/f.py", offset=0, limit=100)

        assert result.start_line == 1
        assert result.end_line == 100
        assert result.next_offset == 100, "a filled window signals more lines may remain"
        assert result.total_lines is None, "the sandbox never reports a total line count"

    async def test_partial_window_reports_end_of_file(self):
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        content = "".join(f"line {i}\n" for i in range(1, 43))  # fewer than `limit` lines
        backend = self._bound_backend(FsReadResponse(content=content, encoding="utf-8"))

        result = await backend.aread(f"{REPO_PATH}/f.py", offset=0, limit=100)

        assert result.start_line == 1
        assert result.end_line == 42
        assert result.next_offset is None, "an unfilled window means EOF was reached"

    async def test_window_is_anchored_at_the_requested_offset(self):
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        content = "".join(f"line {i}\n" for i in range(51, 101))  # 50 lines starting at source line 51
        backend = self._bound_backend(FsReadResponse(content=content, encoding="utf-8"))

        result = await backend.aread(f"{REPO_PATH}/f.py", offset=50, limit=50)

        assert result.start_line == 51
        assert result.end_line == 100
        assert result.next_offset == 100

    async def test_partial_window_at_a_nonzero_offset_reports_eof(self):
        """A later page that comes back unfilled must report EOF with `end_line` anchored at the
        offset — the arithmetic where an offset-ignoring bug would hide."""
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        content = "".join(f"line {i}\n" for i in range(51, 81))  # 30 lines from source line 51
        backend = self._bound_backend(FsReadResponse(content=content, encoding="utf-8"))

        result = await backend.aread(f"{REPO_PATH}/f.py", offset=50, limit=100)

        assert result.start_line == 51
        assert result.end_line == 80
        assert result.next_offset is None

    async def test_contentless_read_carries_no_window(self):
        """Empty content (the `resp.content or ""` coercion can reach the helper) must never
        advertise a window — a zero-line window has nothing to paginate."""
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        backend = self._bound_backend(FsReadResponse(content="", encoding="utf-8"))

        result = await backend.aread(f"{REPO_PATH}/f.py", offset=0, limit=100)

        assert result.start_line is None
        assert result.end_line is None
        assert result.next_offset is None

    async def test_notice_tells_the_model_more_lines_remain(self):
        """End-to-end: the window fields must drive deepagents' actual truncation notice — the
        behaviour the trace showed missing on sandbox reads."""
        from deepagents.middleware.filesystem import _remaining_lines_notice

        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        content = "".join(f"line {i}\n" for i in range(1, 101))
        backend = self._bound_backend(FsReadResponse(content=content, encoding="utf-8"))

        result = await backend.aread(f"{REPO_PATH}/f.py", offset=0, limit=100)

        assert "More lines remain from offset 100" in _remaining_lines_notice(result)

    async def test_binary_read_carries_no_window(self):
        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        backend = self._bound_backend(FsReadResponse(content="Zm9vYmFy", encoding="base64"))

        result = await backend.aread(f"{REPO_PATH}/img.png", offset=0, limit=100)

        assert result.start_line is None
        assert result.end_line is None
        assert result.next_offset is None

    async def test_empty_file_sentinel_carries_no_window(self):
        """The sandbox returns a sentinel string (not the file's bytes) for an empty file; it must
        not be line-windowed, even when `limit` is small enough to trip the full-window heuristic."""
        from deepagents.middleware.filesystem import EMPTY_CONTENT_WARNING

        from automation.agent.constants import REPO_PATH
        from core.sandbox.schemas import FsReadResponse

        backend = self._bound_backend(FsReadResponse(content=EMPTY_CONTENT_WARNING, encoding="utf-8"))

        result = await backend.aread(f"{REPO_PATH}/empty.py", offset=0, limit=1)

        assert result.start_line is None
        assert result.end_line is None
        assert result.next_offset is None
