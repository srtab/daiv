from __future__ import annotations

from typing import TYPE_CHECKING

from deepagents.backends.composite import CompositeBackend
from deepagents.middleware.filesystem import FilesystemMiddleware as UpstreamFilesystemMiddleware
from deepagents.middleware.filesystem import _check_fs_permission

from automation.agent.workspace import backends as backends_module
from automation.agent.workspace.backends import (
    WORKSPACE_FENCE_PERMISSIONS,
    DAIVFilesystemBackend,
    RepoFilesystemBackend,
    SkillsCacheBackend,
    build_disk_workspace_backend,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestDiskBackendRegexGrep:
    def _backend(self, tmp_path: Path):
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
        monkeypatch.setattr(backends_module, "DEFAULT_GREP_TIMEOUT", -1)

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


class TestCompositeRouting:
    """DAIV's regex ``agrep`` still applies when reached through a ``CompositeBackend`` route."""

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
        skills_root = tmp_path / "skills-mount"
        skills_root.mkdir()
        repo_root = tmp_path / "repo-mount"
        repo_root.mkdir()
        skills = DAIVFilesystemBackend(root_dir=skills_root, virtual_mode=True)
        repo = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        composite = CompositeBackend(default=repo, routes={"/skills/": skills})
        return composite, skills, repo, skills_root, repo_root

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


class TestWorkspaceFencePermissions:
    """Disk-mode fence: allow read+write under the repo and tmp subtrees, read-only access to the skills cache and the
    offloaded-artifact dirs, no access to any ``.git`` in the repo, and deny the bare /workspace root plus any other
    path beneath it, dot-named paths included."""

    def test_allows_repo_and_tmp_subtrees(self):
        for op in ("read", "write"):
            for path in (
                "/workspace/repo",
                "/workspace/repo/daiv/foo.py",
                "/workspace/repo/.github/workflows/ci.yml",
                "/workspace/repo/.gitlab-ci.yml",
                "/workspace/repo/.daiv.yml",
                "/workspace/repo/.gitignore",
                "/workspace/repo/.agents/skills/x/SKILL.md",
                "/workspace/repo/src/.env.example",
                "/workspace/repo/.a/.b/c",
                "/workspace/tmp",
                "/workspace/tmp/scratch.txt",
                "/workspace/tmp/.scratch",
            ):
                assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, op, path) == "allow", (op, path)

    def test_denies_git_metadata(self):
        for op in ("read", "write"):
            for path in (
                "/workspace/repo/.git",
                "/workspace/repo/.git/config",
                "/workspace/repo/.git/hooks/pre-commit",
                "/workspace/repo/.git/info/.attributes",
                "/workspace/repo/vendor/lib/.git",
                "/workspace/repo/vendor/lib/.git/config",
                "/workspace/repo/.agents/vendor/.git/config",
                "/workspace/repo/.git/a/.b/.c",
            ):
                assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, op, path) == "deny", (op, path)

    def test_fence_allows_writes_in_extra_repos_but_not_their_git(self):
        for op in ("read", "write"):
            assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, op, "/workspace/repos/g/mod/a.py") == "allow"
            assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, op, "/workspace/repos/g/mod/.git/config") == "deny"
            assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, op, "/workspace/repos/g/mod/vendor/.git") == "deny"

    def test_skills_cache_is_readable_but_not_writable(self):
        for path in (
            "/workspace/skills",
            "/workspace/skills/code-review/SKILL.md",
            "/workspace/skills/x/.hidden",
            "/workspace/skills/.a/.b/c",
        ):
            assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, "read", path) == "allow", path
            assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, "write", path) == "deny", path

    def test_denies_bare_root_and_unrelated_workspace_paths(self):
        # Bare /workspace (the deny needs the literal pattern; /workspace/** does not match it) and
        # any path under /workspace that isn't a real subtree or an artifact dir are denied both ways.
        for op in ("read", "write"):
            for path in (
                "/workspace",
                "/workspace/random",
                "/workspace/repofoo",
                "/workspace/repofoo/x",
                "/workspace/.foo",
                "/workspace/.foo/bar",
                "/workspace/random/.foo",
                "/workspace/.a/.b/c",
            ):
                assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, op, path) == "deny", (op, path)

    def test_artifact_dirs_are_readable_but_not_writable(self):
        # Eviction / output_to_file write here through the backend directly (bypassing the fence);
        # the agent must be able to read the offloaded file back, but never writes here itself.
        for path in (
            "/workspace/large_tool_results",
            "/workspace/large_tool_results/call_abc",
            "/workspace/conversation_history",
            "/workspace/conversation_history/uuid.md",
            "/workspace/large_tool_results/.x/.y",
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

        backend = build_disk_workspace_backend(clone_dir, skills_cache=skills_cache)

        assert (await backend.awrite("/workspace/repo/a.txt", "R")).error is None
        assert (clone_dir / "a.txt").read_text() == "R"

        (upload,) = await backend.aupload_files([("/workspace/skills/s.txt", b"S")])
        assert upload.error is None
        assert (skills_cache / "s.txt").read_text() == "S"

        assert (await backend.awrite("/workspace/tmp/t.txt", "T")).error is None
        assert (clone_dir.parent / "scratch" / "workspace" / "tmp" / "t.txt").read_text() == "T"

    async def test_extra_clone_is_routed_under_its_workspace_path(self, tmp_path):
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()
        (mod_dir / "README.md").write_text("mod readme\n")

        backend = build_disk_workspace_backend(
            clone_dir, skills_cache=tmp_path / "skills_cache", extra_clones={"/workspace/repos/g/mod": mod_dir}
        )

        [response] = await backend.adownload_files(["/workspace/repos/g/mod/README.md"])
        assert response.content == b"mod readme\n"
        assert (await backend.awrite("/workspace/repos/g/mod/a.py", "A")).error is None
        assert (mod_dir / "a.py").read_text() == "A"

    async def test_extra_clone_git_metadata_is_refused(self, tmp_path):
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        mod_dir = _clone_with_git_config(tmp_path / "mod_root")
        backend = build_disk_workspace_backend(
            clone_dir, skills_cache=tmp_path / "skills_cache", extra_clones={"/workspace/repos/g/mod": mod_dir}
        )

        assert (await backend.aedit("/workspace/repos/g/mod/.git/config", "original", "planted")).error is not None
        assert (mod_dir / ".git" / "config").read_text() == "original"

    async def test_unrouted_paths_cannot_reach_the_clone(self, tmp_path):
        clone_dir = _clone_with_git_config(tmp_path)
        backend = build_disk_workspace_backend(clone_dir, skills_cache=tmp_path / "skills_cache")

        assert (await backend.aedit("/repo/.git/config", "original", "planted")).error is not None
        assert (await backend.awrite("/repo/a.txt", "A")).error is None

        assert (clone_dir / ".git" / "config").read_text() == "original"
        assert not (clone_dir / "a.txt").exists()

    def test_artifacts_root_is_workspace(self, tmp_path):
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        backend = build_disk_workspace_backend(clone_dir, skills_cache=tmp_path / "skills_cache")
        assert backend.artifacts_root == "/workspace"


def _clone_with_git_config(tmp_path: Path) -> Path:
    clone_dir = tmp_path / "repo"
    (clone_dir / ".git").mkdir(parents=True)
    (clone_dir / ".git" / "config").write_text("original")
    return clone_dir


class TestRepoFilesystemBackend:
    """Git metadata in the clone is off limits to the file tools at any depth and through any alias."""

    async def test_refuses_git_metadata(self, tmp_path: Path):
        clone_dir = _clone_with_git_config(tmp_path)
        backend = RepoFilesystemBackend(root_dir=clone_dir, virtual_mode=True)

        assert (await backend.awrite("/.git/config", "planted")).error is not None
        assert (await backend.aedit("/.git/config", "original", "planted")).error is not None
        assert (await backend.awrite("/.git/hooks/pre-commit", "planted")).error is not None
        assert (await backend.awrite("/vendor/lib/.git", "gitdir: /tmp/planted")).error is not None
        assert (await backend.aread("/.git/config")).error is not None
        assert (await backend.als("/.git")).error is not None
        (download,) = await backend.adownload_files(["/.git/config"])
        assert download.error is not None

        assert (clone_dir / ".git" / "config").read_text() == "original"
        assert not (clone_dir / ".git" / "hooks").exists()
        assert not (clone_dir / "vendor").exists()

    async def test_refuses_git_metadata_through_a_symlink(self, tmp_path: Path):
        clone_dir = _clone_with_git_config(tmp_path)
        (clone_dir / "alias").symlink_to(".git")
        backend = RepoFilesystemBackend(root_dir=clone_dir, virtual_mode=True)

        assert (await backend.aedit("/alias/config", "original", "planted")).error is not None
        assert (clone_dir / ".git" / "config").read_text() == "original"

    async def test_allows_dot_named_repository_files(self, tmp_path: Path):
        clone_dir = _clone_with_git_config(tmp_path)
        backend = RepoFilesystemBackend(root_dir=clone_dir, virtual_mode=True)

        for path in ("/.github/workflows/ci.yml", "/.gitlab-ci.yml", "/.daiv.yml", "/.gitignore"):
            assert (await backend.awrite(path, "content")).error is None, path
            assert (clone_dir / path.lstrip("/")).read_text() == "content"

    async def test_grep_skips_git_metadata(self, tmp_path: Path):
        clone_dir = _clone_with_git_config(tmp_path)
        (clone_dir / ".git" / "config").write_text("needle")
        (clone_dir / "a.py").write_text("needle")
        backend = RepoFilesystemBackend(root_dir=clone_dir, virtual_mode=True)

        result = await backend.agrep("needle")

        assert [m["path"] for m in result.matches or []] == ["/a.py"]


class TestSkillsCacheBackend:
    """The worker-wide skills cache only grows through ``SkillsMiddleware``'s uploads, so no run can plant a file that
    a later run loads."""

    async def test_refuses_file_tool_writes(self, tmp_path: Path):
        (tmp_path / "code-review").mkdir()
        (tmp_path / "code-review" / "SKILL.md").write_text("original")
        backend = SkillsCacheBackend(root_dir=tmp_path, virtual_mode=True)

        assert (await backend.awrite("/code-review/SKILL.md", "planted")).error is not None
        assert (await backend.awrite("/new-skill/SKILL.md", "planted")).error is not None
        assert (await backend.aedit("/code-review/SKILL.md", "original", "planted")).error is not None
        assert (await backend.adelete("/code-review/SKILL.md")).error is not None

        assert (tmp_path / "code-review" / "SKILL.md").read_text() == "original"
        assert not (tmp_path / "new-skill").exists()

    async def test_accepts_uploads(self, tmp_path: Path):
        backend = SkillsCacheBackend(root_dir=tmp_path, virtual_mode=True)

        (upload,) = await backend.aupload_files([("/code-review/SKILL.md", b"body")])

        assert upload.error is None
        assert (tmp_path / "code-review" / "SKILL.md").read_text() == "body"
