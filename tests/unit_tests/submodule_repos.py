"""Real git repositories with submodules and LFS pointers, served from local bare remotes, for the checkout, git
manager and publisher tests. A stub client maps platform paths to those remotes (research R12)."""

from __future__ import annotations

import hashlib
import os
import subprocess  # noqa: S404
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from git import Repo

from codebase.base import GitPlatform, Repository
from codebase.clients.base import GitAuthEnv

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

PLATFORM_URL = "https://git.example.com"

_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

LFS_ATTRIBUTES = "*.bin filter=lfs diff=lfs merge=lfs -text\n"

CLEAN_FILTER_SCRIPT = """\
import hashlib, sys
data = sys.stdin.buffer.read()
if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
    sys.stdout.buffer.write(data)
else:
    digest = hashlib.sha256(data).hexdigest()
    sys.stdout.write(f"version https://git-lfs.github.com/spec/v1\\noid sha256:{digest}\\nsize {len(data)}\\n")
"""


def git(cwd: Path, *args: str, check: bool = True, env: Mapping[str, str] | None = None) -> str:
    """Run git in ``cwd`` as a test author, allowing ``file://`` submodules."""
    result = subprocess.run(  # noqa: S603
        ["git", "-c", "protocol.file.allow=always", "-c", "init.defaultBranch=main", *args],  # noqa: S607
        cwd=cwd,
        env={**os.environ, **_IDENTITY, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def lfs_pointer(content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return f"version https://git-lfs.github.com/spec/v1\noid sha256:{digest}\nsize {len(content)}\n"


def clean_filter_config(tmp_path: Path) -> dict[str, str]:
    """A stand-in for git-lfs's clean filter: it turns a file's content into its LFS pointer."""
    script = tmp_path / "lfs_clean.py"
    script.write_text(CLEAN_FILTER_SCRIPT)
    return {"filter.lfs.clean": f"{sys.executable} {script}", "filter.lfs.required": "true"}


@dataclass
class World:
    """Bare remotes by platform path, their working copies, and clones of them."""

    root: Path
    remotes: dict[str, Path] = field(default_factory=dict)

    def bare(self, slug: str) -> Path:
        return self.root / "remotes" / f"{slug}.git"

    def work(self, slug: str) -> Path:
        return self.root / "work" / slug

    def create(
        self,
        slug: str,
        files: Mapping[str, str | bytes] | None = None,
        *,
        submodules: Mapping[str, str] | None = None,
        gitmodules_extra: str = "",
    ) -> str:
        """Create ``slug`` with ``files`` and ``submodules`` (path -> URL, relative ones against its own remote), push
        it to its bare remote and return its head commit."""
        bare = self.bare(slug)
        bare.parent.mkdir(parents=True, exist_ok=True)
        git(self.root, "init", "-q", "--bare", str(bare))
        work = self.work(slug)
        work.mkdir(parents=True)
        git(work, "init", "-q")
        git(work, "remote", "add", "origin", str(bare))
        for path, content in (files or {"README.md": f"{slug}\n"}).items():
            target = work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)
        git(work, "add", "-A")
        for path, url in (submodules or {}).items():
            git(work, "submodule", "add", "-q", url, path)
        if gitmodules_extra:
            with (work / ".gitmodules").open("a") as handle:
                handle.write(gitmodules_extra)
            git(work, "add", ".gitmodules")
        git(work, "commit", "-q", "-m", "initial")
        git(work, "push", "-q", "origin", "HEAD:main")
        self.remotes[slug] = bare
        return self.head(slug)

    def commit(self, slug: str, files: Mapping[str, str], *, branch: str = "main", message: str = "change") -> str:
        """Commit ``files`` on ``branch`` of ``slug``'s working copy and push it."""
        work = self.work(slug)
        git(
            work,
            "checkout",
            "-q",
            "-B",
            branch,
            f"origin/{branch}" if self._has_remote_branch(slug, branch) else "HEAD",
        )
        for path, content in files.items():
            (work / path).parent.mkdir(parents=True, exist_ok=True)
            (work / path).write_text(content)
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", message)
        git(work, "push", "-q", "origin", f"HEAD:{branch}")
        return self.head(slug)

    def _has_remote_branch(self, slug: str, branch: str) -> bool:
        git(self.work(slug), "fetch", "-q", "origin")
        return bool(git(self.work(slug), "branch", "-r", "--list", f"origin/{branch}").strip())

    def head(self, slug: str, ref: str = "HEAD") -> str:
        return git(self.work(slug), "rev-parse", ref).strip()

    def clone(self, slug: str, *, branch: str = "main") -> Repo:
        """A clone of ``slug`` without its submodules, as ``load_repo`` makes it."""
        target = self.root / "clones" / slug
        target.parent.mkdir(parents=True, exist_ok=True)
        git(self.root, "clone", "-q", "--branch", branch, str(self.bare(slug)), str(target))
        repo = Repo(target)
        with repo.config_writer() as writer:
            writer.set_value("user", "name", "Test")
            writer.set_value("user", "email", "test@example.com")
        return repo

    def repository(self, slug: str) -> Repository:
        """``slug`` as the platform describes it: an HTTPS clone URL on the platform's host."""
        return _repository(slug, f"{PLATFORM_URL}/{slug}.git")


def _repository(slug: str, clone_url: str) -> Repository:
    return Repository(
        pk=abs(hash(slug)) % 10_000,
        slug=slug,
        name=slug.rsplit("/", 1)[-1],
        clone_url=clone_url,
        html_url=f"{PLATFORM_URL}/{slug}",
        default_branch="main",
        git_platform=GitPlatform.GITLAB,
    )


@dataclass
class StubClient:
    """A platform client over ``world``'s remotes: a lookup answers with the remote's ``file://`` URL as its clone URL,
    and each mint hands the operation a credential naming the repository."""

    world: World
    unreadable: set[str] = field(default_factory=set)
    git_platform: GitPlatform = GitPlatform.GITLAB
    auth_calls: list[tuple[str, bool]] = field(default_factory=list)
    auth_error: Callable[[Repository], Exception | None] | None = None

    def get_repository(self, repo_id: str) -> Repository:
        if repo_id in self.unreadable or repo_id not in self.world.remotes:
            raise RuntimeError(f"404 {repo_id}")
        return _repository(repo_id, self.world.bare(repo_id).as_uri())

    def with_git_auth(
        self, repository: Repository, operation: Callable[[GitAuthEnv | None], Any], *, scoped_only=False
    ):
        self.auth_calls.append((repository.slug, scoped_only))
        if self.auth_error is not None and (error := self.auth_error(repository)) is not None:
            raise error
        return operation(GitAuthEnv.for_token(repository.clone_url, f"tok-{repository.slug}"))


@dataclass
class Access:
    """A ``RunRepoAccess`` answering from sets; ``None`` allows every repository."""

    readable: set[str] | None = None
    writable: set[str] | None = None
    catalogued: set[str] | None = None

    def can_read(self, repo_id: str) -> bool:
        return self.readable is None or repo_id in self.readable

    def can_write(self, repo_id: str) -> bool:
        return self.writable is None or repo_id in self.writable

    def in_catalog(self, repo_id: str) -> bool:
        return self.catalogued is None or repo_id in self.catalogued


def app_with_nested_lib(world: World) -> dict[str, str]:
    """``group/app`` with ``libs/lib`` (``group/lib``), itself holding ``vendor/inner`` (``group/inner``); returns
    each repository's head commit."""
    heads = {"group/inner": world.create("group/inner", {"inner.c": "int inner;\n"})}
    heads["group/lib"] = world.create("group/lib", {"lib.h": "lib\n"}, submodules={"vendor/inner": "../inner.git"})
    heads["group/app"] = world.create("group/app", {"README.md": "app\n"}, submodules={"libs/lib": "../lib.git"})
    return heads
