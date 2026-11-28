from __future__ import annotations

import logging
import tarfile
import time
from io import BytesIO
from pathlib import Path

import pytest
from git import GitCommandError

from codebase.clients.base import GitAuthEnv
from codebase.clients.checkout import (
    DENY_ALL,
    LFS_ARGV_LIMIT,
    CheckoutOptions,
    CheckoutReport,
    CommandResult,
    PublishBlock,
    RepoCheckout,
    SkipReason,
    SubmoduleNote,
    _SubmoduleWalk,
)
from tests.unit_tests.submodule_repos import (
    LFS_ATTRIBUTES,
    Access,
    StubClient,
    World,
    app_with_nested_lib,
    clean_filter_config,
    git,
    lfs_pointer,
)

SUBMODULES = CheckoutOptions(submodules=True)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


class Recorder:
    """Wraps a checkout's command runner, recording each command, and answering ``git-lfs`` itself: ``pull`` writes
    each included file's content from ``lfs_content``, ``smudge`` returns it."""

    def __init__(self, checkout: RepoCheckout, lfs_content: dict[str, bytes] | None = None, lfs_error=None):
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []
        self.lfs_content = lfs_content or {}
        self.lfs_error = lfs_error
        self._run = checkout.run_command
        checkout.run_command = self

    def __call__(self, argv, cwd, env, stdin):
        self.calls.append((list(argv), Path(cwd), dict(env)))
        if argv[0] == "git-lfs":
            if self.lfs_error is not None:
                if isinstance(self.lfs_error, BaseException):
                    raise self.lfs_error
                return CommandResult(1, b"", self.lfs_error)
            if argv[1] == "smudge":
                return CommandResult(0, self.lfs_content[argv[-1]], "")
            include = argv[2].removeprefix("--include=")
            for pattern in _split_include(include):
                path = pattern.replace("\\", "")
                (Path(cwd) / path).write_bytes(self.lfs_content[path])
            return CommandResult(0, b"", "")
        return self._run(argv, cwd, env, stdin)

    def git_args(self) -> list[list[str]]:
        return [argv[5:] for argv, _, _ in self.calls if argv[0] == "git"]

    def lfs_calls(self) -> list[list[str]]:
        return [argv for argv, _, _ in self.calls if argv[0] == "git-lfs"]


def _split_include(include: str) -> list[str]:
    patterns, current, escaped = [], "", False
    for char in include:
        if escaped:
            current += "\\" + char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ",":
            patterns.append(current)
            current = ""
        else:
            current += char
    patterns.append(current)
    return patterns


def _checkout(world, options=SUBMODULES, access=None, client=None, **kwargs) -> RepoCheckout:
    return RepoCheckout(
        client or StubClient(world), options=options, access=access or Access(), allowed_protocol="file", **kwargs
    )


def _complete(world, checkout, slug="group/app", *, base_branch="main"):
    repo = world.clone(slug)
    return repo, checkout.complete(repo, world.repository(slug), parent_base_branch=base_branch)


class TestOff:
    def test_off_returns_the_empty_report_and_runs_no_git(self, world):
        world.create("group/app")
        checkout = _checkout(world, options=CheckoutOptions.off())
        recorder = Recorder(checkout)

        _, report = _complete(world, checkout)

        assert report is CheckoutReport.EMPTY
        assert report.is_empty
        assert recorder.calls == []

    def test_a_report_with_either_setting_on_is_not_empty(self):
        assert not CheckoutReport(CheckoutOptions(lfs=True)).is_empty


class TestUrlResolution:
    @pytest.fixture
    def walk(self, world):
        checkout = _checkout(world)
        return _SubmoduleWalk(checkout, world.root, world.repository("group/app"))

    @pytest.mark.parametrize(
        "url",
        [
            "../lib.git",
            "https://git.example.com/group/lib.git",
            "git@git.example.com:group/lib.git",
            "ssh://git@git.example.com:2222/group/lib.git",
            "https://user:secret@git.example.com/group/lib",
        ],
    )
    def test_platform_urls_resolve_to_the_repository_path(self, walk, url):
        repo_id, resolved = walk.resolve(url, "https://git.example.com/group/app.git")

        assert repo_id == "group/lib"
        assert resolved == "https://git.example.com/group/lib.git"

    def test_a_nested_relative_url_resolves_against_its_parents_resolved_url(self, walk):
        assert walk.resolve("../inner.git", "https://git.example.com/group/lib.git")[0] == "group/inner"
        assert walk.resolve("../../other/x.git", "https://git.example.com/group/lib.git")[0] == "other/x"

    @pytest.mark.parametrize(
        "url",
        [
            "https://other.example.com/x.git",
            "file:///tmp/x",
            "/srv/x",
            "ext::sh -c x",
            "git://git.example.com/x.git",
            "git@other.example.com:group/lib.git",
        ],
    )
    def test_anything_else_is_outside_the_platform(self, walk, url):
        assert walk.resolve(url, "https://git.example.com/group/app.git") is None

    def test_a_platform_under_a_path_prefix_keeps_it(self, world):
        repository = world.repository("group/app").model_copy(
            update={"clone_url": "https://git.example.com/gitlab/group/app.git"}
        )
        walk = _SubmoduleWalk(_checkout(world), world.root, repository)

        assert walk.resolve("../lib.git", "https://git.example.com/gitlab/group/app.git") == (
            "group/lib",
            "https://git.example.com/gitlab/group/lib.git",
        )
        assert walk.resolve("git@git.example.com:group/lib.git", "") == (
            "group/lib",
            "https://git.example.com/gitlab/group/lib.git",
        )


class TestSubmoduleCheckout:
    def test_nested_submodules_are_checked_out_at_the_recorded_commits(self, world):
        heads = app_with_nested_lib(world)
        world.commit("group/lib", {"lib.h": "newer\n"})
        world.commit("group/inner", {"inner.c": "newer\n"})

        repo, report = _complete(world, _checkout(world))
        top = Path(repo.working_dir)

        assert [(s.path, s.parent_path, s.skipped) for s in report.submodules] == [
            ("libs/lib", "", None),
            ("libs/lib/vendor/inner", "libs/lib", None),
        ]
        assert (top / "libs/lib/lib.h").read_text() == "lib\n"
        assert (top / "libs/lib/vendor/inner/inner.c").read_text() == "int inner;\n"
        assert git(top / "libs/lib", "rev-parse", "HEAD").strip() == heads["group/lib"]
        assert report.submodules[0].repository.slug == "group/lib"
        assert git(top, "status", "--porcelain") == ""

    def test_the_url_is_rewritten_to_the_repository_clone_url(self, world):
        app_with_nested_lib(world)

        repo, _ = _complete(world, _checkout(world))
        top = Path(repo.working_dir)

        clone_url = world.bare("group/lib").as_uri()
        assert git(top, "config", "submodule.libs/lib.url").strip() == clone_url
        assert git(top / "libs/lib", "remote", "get-url", "origin").strip() == clone_url

    def test_a_shallow_submodule_is_cloned_whole_with_every_branch(self, world):
        world.create("group/lib", {"lib.h": "lib\n"})
        world.commit("group/lib", {"lib.h": "release\n"}, branch="release")
        world.create("group/app", submodules={"libs/lib": "../lib.git"}, gitmodules_extra="\tshallow = true\n")

        repo, report = _complete(world, _checkout(world))
        sub = Path(repo.working_dir) / "libs/lib"

        assert report.submodules[0].skipped is None
        assert git(sub, "rev-parse", "--is-shallow-repository").strip() == "false"
        assert "origin/release" in git(sub, "branch", "-r")

    def test_the_update_runs_with_hooks_off_and_overrides_the_update_mode(self, world):
        world.create("group/lib")
        world.create("group/app", submodules={"libs/lib": "../lib.git"}, gitmodules_extra="\tupdate = rebase\n")
        checkout = _checkout(world)
        recorder = Recorder(checkout)

        _, report = _complete(world, checkout)

        assert report.submodules[0].skipped is None
        update = next(args for args in recorder.git_args() if args[:2] == ["submodule", "update"])
        assert update == ["submodule", "update", "--checkout", "--no-recommend-shallow", "--", "libs/lib"]
        assert all(argv[1:3] == ["-c", "core.hooksPath=/dev/null"] for argv, _, _ in recorder.calls)

    def test_a_submodule_command_from_gitmodules_never_runs(self, world, tmp_path):
        marker = tmp_path / "pwned"
        world.create("group/lib")
        world.create(
            "group/app", submodules={"libs/lib": "../lib.git"}, gitmodules_extra=f"\tupdate = !touch {marker}\n"
        )

        _, report = _complete(world, _checkout(world))

        assert not marker.exists()
        assert report.submodules[0].skipped is SkipReason.FETCH_FAILED

    def test_an_outside_url_is_reported_with_its_user_info_stripped_and_never_contacted(self, world):
        world.create("group/app")
        work = world.work("group/app")
        git(work, "submodule", "add", "-q", str(world.root / "remotes/group/app.git"), "vendor/self")
        git(
            work,
            "config",
            "-f",
            ".gitmodules",
            "submodule.vendor/self.url",
            "https://user:token@other.example.com/x.git",
        )
        git(work, "commit", "-qam", "outside")
        git(work, "push", "-q", "origin", "HEAD:main")
        client = StubClient(world)
        checkout = _checkout(world, client=client)
        recorder = Recorder(checkout)

        _, report = _complete(world, checkout)

        (sub,) = report.submodules
        assert (sub.skipped, sub.repo_id, sub.url) == (
            SkipReason.OUTSIDE_PLATFORM,
            None,
            "https://other.example.com/x.git",
        )
        assert client.auth_calls == []
        assert not any(args[:1] == ["submodule"] for args in recorder.git_args())


class TestAccess:
    def test_outside_the_catalog_nothing_is_looked_up_or_fetched(self, world):
        app_with_nested_lib(world)
        client = StubClient(world)
        client.get_repository = None  # a lookup would fail the test

        repo, report = _complete(world, _checkout(world, access=Access(catalogued=set()), client=client))

        assert [(s.path, s.skipped) for s in report.submodules] == [("libs/lib", SkipReason.NOT_CATALOGUED)]
        assert list((Path(repo.working_dir) / "libs/lib").iterdir()) == []

    def test_a_repository_daivs_account_cant_read_stays_empty(self, world):
        app_with_nested_lib(world)
        client = StubClient(world, unreadable={"group/lib"})

        repo, report = _complete(world, _checkout(world, client=client))

        assert [(s.path, s.skipped) for s in report.submodules] == [("libs/lib", SkipReason.DAIV_NO_ACCESS)]
        assert client.auth_calls == []
        assert list((Path(repo.working_dir) / "libs/lib").iterdir()) == []

    def test_a_repository_the_person_cant_read_stays_empty_with_nothing_below_it(self, world):
        app_with_nested_lib(world)
        client = StubClient(world)

        repo, report = _complete(
            world, _checkout(world, access=Access(readable={"group/app", "group/inner"}), client=client)
        )

        assert [(s.path, s.skipped) for s in report.submodules] == [("libs/lib", SkipReason.PERSON_NO_ACCESS)]
        assert client.auth_calls == []
        assert list((Path(repo.working_dir) / "libs/lib").iterdir()) == []

    def test_deny_all_checks_out_nothing(self, world):
        app_with_nested_lib(world)

        _, report = _complete(world, _checkout(world, access=DENY_ALL))

        assert report.checked_out_submodules == ()


class TestCredentials:
    def test_each_submodule_is_fetched_with_its_own_scoped_credential_and_protocol(self, world):
        app_with_nested_lib(world)
        client = StubClient(world)
        checkout = _checkout(world, client=client)
        recorder = Recorder(checkout)

        _complete(world, checkout)

        assert client.auth_calls == [("group/lib", True), ("group/inner", True)]
        headers = {
            slug: GitAuthEnv.for_token(world.bare(slug).as_uri(), f"tok-{slug}").header.get_secret_value()
            for slug in ("group/app", "group/lib", "group/inner")
        }
        updates = [(cwd, env) for argv, cwd, env in recorder.calls if argv[5:7] == ["submodule", "update"]]
        assert [cwd.name for cwd, _ in updates] == [["group", "app"][-1], "lib"]
        for (_, env), slug in zip(updates, ("group/lib", "group/inner"), strict=True):
            assert env["GIT_CONFIG_COUNT"] == "1"
            assert env["GIT_CONFIG_VALUE_0"] == headers[slug]
            assert headers["group/app"] not in env.values()
            assert env["GIT_ALLOW_PROTOCOL"] == "file"
            assert env["GIT_LFS_SKIP_SMUDGE"] == "1"

    def test_only_the_inherited_environment_reaches_git(self, world, monkeypatch):
        monkeypatch.setenv("DJANGO_SECRET_KEY", "s3cret")
        app_with_nested_lib(world)
        checkout = _checkout(world)
        recorder = Recorder(checkout)

        _complete(world, checkout)

        assert all("DJANGO_SECRET_KEY" not in env for _, _, env in recorder.calls)


class TestPublishVerdictAndTarget:
    def test_a_person_who_cant_write_gets_a_read_only_submodule(self, world):
        app_with_nested_lib(world)

        _, report = _complete(world, _checkout(world, access=Access(writable={"group/inner"})))

        assert [(s.path, s.publish_block) for s in report.submodules] == [
            ("libs/lib", PublishBlock.PERSON_NO_WRITE),
            ("libs/lib/vendor/inner", None),
        ]

    def test_the_target_is_the_branch_gitmodules_names_when_the_remote_has_it(self, world):
        world.create("group/lib")
        world.commit("group/lib", {"x": "1"}, branch="stable")
        world.create("group/app", submodules={"libs/lib": "../lib.git"}, gitmodules_extra="\tbranch = stable\n")

        _, report = _complete(world, _checkout(world))

        assert report.submodules[0].target_branch == "stable"

    def test_dot_means_the_parents_base_branch(self, world):
        world.create("group/lib")
        world.commit("group/lib", {"x": "1"}, branch="feature")
        world.create("group/app", submodules={"libs/lib": "../lib.git"}, gitmodules_extra="\tbranch = .\n")

        _, report = _complete(world, _checkout(world), base_branch="feature")

        assert report.submodules[0].target_branch == "feature"

    @pytest.mark.parametrize("extra", ["", "\tbranch = gone\n"], ids=["unnamed", "missing-on-remote"])
    def test_otherwise_the_default_branch(self, world, extra):
        world.create("group/lib")
        world.create("group/app", submodules={"libs/lib": "../lib.git"}, gitmodules_extra=extra)

        _, report = _complete(world, _checkout(world))

        assert report.submodules[0].target_branch == "main"


class TestHints:
    def _app(self, world):
        world.create("group/lib", {"lib.h": "lib\n"})
        recorded = world.head("group/lib")
        world.create("group/app", submodules={"libs/lib": "../lib.git"})
        return recorded

    def test_a_working_branch_that_contains_the_recorded_commit_is_checked_out(self, world):
        recorded = self._app(world)
        world.work("group/lib")
        git(world.work("group/lib"), "checkout", "-q", "-b", "daiv/fix")
        tip = world.commit("group/lib", {"lib.h": "fix\n"}, branch="daiv/fix")

        repo, report = _complete(
            world, _checkout(world, options=CheckoutOptions(submodules=True, branches={"libs/lib": "daiv/fix"}))
        )
        sub = Path(repo.working_dir) / "libs/lib"

        assert (report.submodules[0].branch, report.submodules[0].note) == ("daiv/fix", "")
        assert git(sub, "rev-parse", "--abbrev-ref", "HEAD").strip() == "daiv/fix"
        assert git(sub, "rev-parse", "HEAD").strip() == tip != recorded

    def test_a_branch_that_moved_on_leaves_it_detached_with_a_note(self, world):
        self._app(world)
        work = world.work("group/lib")
        git(work, "checkout", "-q", "--orphan", "daiv/fix")
        git(work, "commit", "-q", "-m", "unrelated history")
        git(work, "push", "-q", "origin", "HEAD:daiv/fix")

        repo, report = _complete(
            world, _checkout(world, options=CheckoutOptions(submodules=True, branches={"libs/lib": "daiv/fix"}))
        )

        assert (report.submodules[0].branch, report.submodules[0].note) == ("", SubmoduleNote.BRANCH_MOVED_ON)
        assert git(Path(repo.working_dir) / "libs/lib", "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"

    def test_a_missing_branch_leaves_it_detached_without_a_note(self, world):
        self._app(world)

        _, report = _complete(
            world, _checkout(world, options=CheckoutOptions(submodules=True, branches={"libs/lib": "gone"}))
        )

        assert (report.submodules[0].branch, report.submodules[0].note) == ("", "")

    def test_a_pin_on_the_recorded_commit_is_applied_without_a_checkout(self, world):
        recorded = self._app(world)
        merged = "f" * 40

        repo, report = _complete(
            world, _checkout(world, options=CheckoutOptions(submodules=True, pins={"libs/lib": (recorded, merged)}))
        )

        assert (report.submodules[0].pinned, report.submodules[0].note) == (merged, "")
        assert git(Path(repo.working_dir) / "libs/lib", "rev-parse", "HEAD").strip() == recorded

    def test_a_pin_on_another_commit_is_left_with_a_note(self, world):
        self._app(world)

        _, report = _complete(
            world, _checkout(world, options=CheckoutOptions(submodules=True, pins={"libs/lib": ("a" * 40, "f" * 40)}))
        )

        assert (report.submodules[0].pinned, report.submodules[0].note) == ("", SubmoduleNote.PIN_LEFT)

    def test_a_failing_hint_leaves_it_detached_with_no_pin_or_note(self, world):
        recorded = self._app(world)
        git(world.work("group/lib"), "checkout", "-q", "-b", "daiv/fix")
        world.commit("group/lib", {"lib.h": "fix\n"}, branch="daiv/fix")
        checkout = _checkout(world, options=CheckoutOptions(submodules=True, branches={"libs/lib": "daiv/fix"}))
        recorder = Recorder(checkout)
        original = recorder._run

        def failing(argv, cwd, env, stdin):
            if "checkout" in argv and "-B" in argv:
                return CommandResult(128, b"", "fatal: broken")
            return original(argv, cwd, env, stdin)

        recorder._run = failing

        repo, report = _complete(world, checkout)

        assert (report.submodules[0].branch, report.submodules[0].pinned, report.submodules[0].note) == ("", "", "")
        assert git(Path(repo.working_dir) / "libs/lib", "rev-parse", "HEAD").strip() == recorded


class TestFailures:
    def test_a_missing_commit_is_reported_and_the_others_still_check_out(self, world):
        world.create("group/lib")
        world.create("group/ui", {"ui.h": "ui\n"})
        world.create("group/app", submodules={"libs/lib": "../lib.git", "libs/ui": "../ui.git"})
        git(world.work("group/app") / "libs/lib", "commit", "-q", "--allow-empty", "-m", "never pushed")
        git(world.work("group/app"), "commit", "-qam", "point at an unpushed commit")
        git(world.work("group/app"), "push", "-q", "origin", "HEAD:main")

        repo, report = _complete(world, _checkout(world))

        assert [(s.path, s.skipped) for s in report.submodules] == [
            ("libs/lib", SkipReason.COMMIT_MISSING),
            ("libs/ui", None),
        ]
        assert (Path(repo.working_dir) / "libs/ui/ui.h").exists()

    @pytest.mark.parametrize(
        "error",
        [GitCommandError(["git"], 128, "fatal: Authentication failed"), RuntimeError("mint failed"), OSError("down")],
        ids=["auth", "mint", "network"],
    )
    def test_a_failed_fetch_is_reported_and_logged(self, world, caplog, error):
        app_with_nested_lib(world)
        client = StubClient(world, auth_error=lambda repository: error)

        with caplog.at_level(logging.INFO, logger="daiv.clients"):
            _, report = _complete(world, _checkout(world, client=client))

        assert [(s.path, s.skipped) for s in report.submodules] == [("libs/lib", SkipReason.FETCH_FAILED)]
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors and "libs/lib" in errors[0].getMessage() and "group/lib" in errors[0].getMessage()

    def test_an_unexpected_failure_in_one_submodule_leaves_the_rest(self, world, caplog, monkeypatch):
        world.create("group/lib")
        world.create("group/ui", {"ui.h": "ui\n"})
        world.create("group/app", submodules={"libs/lib": "../lib.git", "libs/ui": "../ui.git"})
        original = _SubmoduleWalk._target_branch

        def boom(self, sub_dir, *args):
            if sub_dir.name == "lib":
                raise ValueError("unexpected")
            return original(self, sub_dir, *args)

        monkeypatch.setattr(_SubmoduleWalk, "_target_branch", boom)

        with caplog.at_level(logging.ERROR, logger="daiv.clients"):
            _, report = _complete(world, _checkout(world))

        assert [(s.path, s.skipped) for s in report.submodules] == [
            ("libs/lib", SkipReason.FETCH_FAILED),
            ("libs/ui", None),
        ]
        assert any(r.exc_info for r in caplog.records)

    def test_complete_never_raises(self, world, monkeypatch, caplog):
        app_with_nested_lib(world)
        monkeypatch.setattr(_SubmoduleWalk, "run", lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))

        with caplog.at_level(logging.ERROR, logger="daiv.clients"):
            _, report = _complete(world, _checkout(world))

        assert report.submodules == ()
        assert "part-way" in caplog.text

    def test_log_levels_follow_who_can_act_and_carry_no_token(self, world, caplog):
        world.create("group/lib")
        world.create("group/ui")
        world.create("group/priv")
        world.create("group/ro")
        world.create(
            "group/app",
            submodules={
                "a-unreadable": "../lib.git",
                "b-outside": str(world.bare("group/ui")),
                "c-uncatalogued": "../ui.git",
                "d-private": "../priv.git",
                "e-readonly": "../ro.git",
            },
        )
        git(world.work("group/app"), "config", "-f", ".gitmodules", "submodule.b-outside.url", "https://elsewhere/x")
        git(world.work("group/app"), "commit", "-qam", "outside")
        git(world.work("group/app"), "push", "-q", "origin", "HEAD:main")
        client = StubClient(world, unreadable={"group/lib"})
        access = Access(
            catalogued={"group/lib", "group/priv", "group/ro"}, readable={"group/lib", "group/ro"}, writable=set()
        )

        with caplog.at_level(logging.INFO, logger="daiv.clients"):
            _complete(world, _checkout(world, access=access, client=client))

        by_level = {logging.ERROR: [], logging.INFO: []}
        for record in caplog.records:
            by_level.setdefault(record.levelno, []).append(record.getMessage())
        assert any("a-unreadable" in m and "group/lib" in m for m in by_level[logging.ERROR])
        for path in ("b-outside", "c-uncatalogued", "d-private", "e-readonly"):
            assert any(path in m for m in by_level[logging.INFO]), path
            assert not any(path in m for m in by_level[logging.ERROR]), path
        assert "tok-" not in caplog.text


class TestLfs:
    def _repo(self, world, files: dict[str, bytes], **kwargs):
        pointers = {path: lfs_pointer(content) for path, content in files.items()}
        world.create("group/app", {".gitattributes": LFS_ATTRIBUTES, "README.md": "r\n", **pointers}, **kwargs)
        return files

    def test_pointers_are_selected_in_path_order_under_the_cap(self, world):
        files = self._repo(world, {"a/1.bin": b"x" * 10, "b/2.bin": b"y" * 30, "c/3.bin": b"z" * 5})
        checkout = _checkout(world, options=CheckoutOptions(lfs=True, lfs_max_bytes=20))
        recorder = Recorder(checkout, lfs_content=files)

        repo, report = _complete(world, checkout)

        assert [(f.path, f.size, f.skipped) for f in report.lfs_files] == [
            ("a/1.bin", 10, None),
            ("b/2.bin", 30, SkipReason.OVER_LIMIT),
            ("c/3.bin", 5, None),
        ]
        assert recorder.lfs_calls()[0][:3] == ["git-lfs", "pull", "--include=a/1.bin,c/3.bin"]
        assert (Path(repo.working_dir) / "a/1.bin").read_bytes() == b"x" * 10
        assert (Path(repo.working_dir) / "b/2.bin").read_text().startswith("version https://git-lfs")

    def test_a_zero_cap_downloads_nothing(self, world):
        self._repo(world, {"a.bin": b"x"})
        checkout = _checkout(world, options=CheckoutOptions(lfs=True, lfs_max_bytes=0))
        recorder = Recorder(checkout)

        _, report = _complete(world, checkout)

        assert [f.skipped for f in report.lfs_files] == [SkipReason.OVER_LIMIT]
        assert recorder.lfs_calls() == []

    def test_a_blob_committed_without_lfs_is_skipped(self, world):
        world.create("group/app", {".gitattributes": LFS_ATTRIBUTES, "raw.bin": "not a pointer\n"})

        _, report = _complete(world, _checkout(world, options=CheckoutOptions(lfs=True, lfs_max_bytes=100)))

        assert report.lfs_files == ()

    def test_paths_are_escaped_batched_and_a_comma_is_smudged_alone(self, world, monkeypatch):
        files = self._repo(world, {"a[1]*.bin": b"1", "b?.bin": b"2", "c,d.bin": b"3"})
        monkeypatch.setattr("codebase.clients.checkout.LFS_ARGV_LIMIT", 12)
        checkout = _checkout(world, options=CheckoutOptions(lfs=True, lfs_max_bytes=100))
        recorder = Recorder(checkout, lfs_content=files)

        repo, report = _complete(world, checkout)

        calls = recorder.lfs_calls()
        assert ["git-lfs", "pull", "--include=a\\[1\\]\\*.bin", "--exclude="] in calls
        assert ["git-lfs", "pull", "--include=b\\?.bin", "--exclude="] in calls
        assert ["git-lfs", "smudge", "--", "c,d.bin"] in calls
        assert (Path(repo.working_dir) / "c,d.bin").read_bytes() == b"3"
        assert all(f.skipped is None for f in report.lfs_files)

    def test_batches_stay_under_the_argv_limit(self, world):
        files = {f"dir/{'n' * 200}{i:04}.bin": b"x" for i in range(400)}
        self._repo(world, files)
        checkout = _checkout(world, options=CheckoutOptions(lfs=True, lfs_max_bytes=10_000))
        recorder = Recorder(checkout, lfs_content=files)

        _complete(world, checkout)

        assert len(recorder.lfs_calls()) > 1
        assert all(len(argv[2].encode()) < LFS_ARGV_LIMIT for argv in recorder.lfs_calls())

    def test_lfs_comes_from_the_platform_endpoint_whatever_lfsconfig_says(self, world):
        files = self._repo(world, {"a.bin": b"x"})
        git(world.work("group/app"), "config", "-f", ".lfsconfig", "lfs.url", "https://evil.example.com/lfs")
        git(world.work("group/app"), "add", ".lfsconfig")
        git(world.work("group/app"), "commit", "-qm", "lfsconfig")
        git(world.work("group/app"), "push", "-q", "origin", "HEAD:main")
        filter_config = {"filter.lfs.clean": "stand-in"}
        checkout = _checkout(
            world, options=CheckoutOptions(lfs=True, lfs_max_bytes=10), lfs_filter_config=filter_config
        )
        recorder = Recorder(checkout, lfs_content=files)

        _complete(world, checkout)

        (env,) = [env for argv, _, env in recorder.calls if argv[0] == "git-lfs"]
        config = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
        assert config["lfs.url"] == "https://git.example.com/group/app.git/info/lfs"
        assert config["filter.lfs.clean"] == "stand-in"
        assert "GIT_LFS_SKIP_SMUDGE" not in env

    def test_submodule_lfs_files_follow_the_superproject_and_use_their_own_credential(self, world):
        world.create("group/lib", {".gitattributes": LFS_ATTRIBUTES, "m.bin": lfs_pointer(b"lib")})
        files = self._repo(world, {"z.bin": b"app"}, submodules={"libs/lib": "../lib.git"})
        client = StubClient(world)
        checkout = _checkout(world, options=CheckoutOptions(submodules=True, lfs=True, lfs_max_bytes=3), client=client)
        Recorder(checkout, lfs_content={**files, "m.bin": b"lib"})

        _, report = _complete(world, checkout)

        assert [(f.path, f.skipped) for f in report.lfs_files] == [
            ("z.bin", None),
            ("libs/lib/m.bin", SkipReason.OVER_LIMIT),
        ]
        assert ("group/app", False) in client.auth_calls

    @pytest.mark.parametrize("error", ["fatal: batch failed", FileNotFoundError("git-lfs")], ids=["batch", "missing"])
    def test_a_failed_download_marks_its_files_and_logs(self, world, caplog, error):
        files = self._repo(world, {"a.bin": b"x", "b.bin": b"y"})
        checkout = _checkout(world, options=CheckoutOptions(lfs=True, lfs_max_bytes=10))
        Recorder(checkout, lfs_content=files, lfs_error=error)

        with caplog.at_level(logging.ERROR, logger="daiv.clients"):
            _, report = _complete(world, checkout)

        assert [f.skipped for f in report.lfs_files] == [SkipReason.FETCH_FAILED, SkipReason.FETCH_FAILED]
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        if isinstance(error, FileNotFoundError):
            assert "git-lfs" in errors[0]


class TestLfsStat:
    def test_git_status_stays_clean_without_an_lfs_filter_after_the_copy(self, world, tmp_path):
        from automation.agent.workspace.session import _make_repo_archive

        content = b"real content\n" * 100
        world.create("group/app", {".gitattributes": LFS_ATTRIBUTES, "assets/a.bin": lfs_pointer(content)})
        checkout = _checkout(
            world,
            options=CheckoutOptions(lfs=True, lfs_max_bytes=10_000),
            lfs_filter_config=clean_filter_config(tmp_path),
        )
        Recorder(checkout, lfs_content={"assets/a.bin": content})

        repo, _ = _complete(world, checkout)
        top = Path(repo.working_dir)

        assert git(top, "config", "core.checkStat").strip() == "minimal"
        assert git(top, "config", "core.trustctime").strip() == "false"
        assert (top / "assets/a.bin").stat().st_mtime <= time.time() - 2

        copy = tmp_path / "copy"
        with tarfile.open(fileobj=BytesIO(_make_repo_archive(str(top)))) as archive:
            archive.extractall(copy, filter="tar")
        bare_env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path / "empty-home")}
        assert git(copy, "status", "--porcelain", env=bare_env) == ""

        (copy / "assets/a.bin").write_bytes(b"edited")
        (copy / "assets/new.bin").write_bytes(b"new")
        assert git(copy, "status", "--porcelain", "-uall", env=bare_env).splitlines() == [
            " M assets/a.bin",
            "?? assets/new.bin",
        ]

    def test_a_submodule_with_fetched_content_gets_its_own_settings(self, world, tmp_path):
        world.create("group/lib", {".gitattributes": LFS_ATTRIBUTES, "m.bin": lfs_pointer(b"lib")})
        world.create("group/app", submodules={"libs/lib": "../lib.git"})
        checkout = _checkout(
            world,
            options=CheckoutOptions(submodules=True, lfs=True, lfs_max_bytes=10),
            lfs_filter_config=clean_filter_config(tmp_path),
        )
        Recorder(checkout, lfs_content={"m.bin": b"lib"})

        repo, _ = _complete(world, checkout)
        sub = Path(repo.working_dir) / "libs/lib"

        assert git(sub, "config", "--local", "core.checkStat").strip() == "minimal"
        assert git(Path(repo.working_dir), "config", "--local", "core.checkStat", check=False).strip() == ""
        assert git(Path(repo.working_dir), "status", "--porcelain") == ""
        assert Path(sub / "m.bin").stat().st_mtime <= time.time() - 2
