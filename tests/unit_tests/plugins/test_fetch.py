import os
import pathlib
import subprocess  # noqa: S404
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from plugins.conf import settings
from plugins.fetch import (
    FetchError,
    RepoLocation,
    materialize,
    parse_repo_url,
    prune_revisions,
    resolve_head,
    revision_dir,
)
from pydantic import SecretStr

from codebase.clients.base import GitAuthEnv

SHA = "a" * 40
PUBLIC_ADDR = [(2, 1, 6, "", ("93.184.216.34", 0))]


@pytest.fixture
def data_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_PATH", tmp_path)
    return tmp_path


@pytest.fixture
def client():
    client = MagicMock()
    client.git_host.return_value = "git.example.com"
    with patch("plugins.fetch.RepoClient.create_instance", return_value=client):
        yield client


@pytest.fixture(autouse=True)
def public_dns():
    with patch("plugins.fetch.socket.getaddrinfo", return_value=PUBLIC_ADDR) as mock:
        yield mock


def _completed(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fake_git(files: dict[str, str] | None = None, head: str = SHA):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "clone" in args:
            target = pathlib.Path(args[-1])
            (target / ".git").mkdir(exist_ok=True, parents=True)
            for rel, content in (files or {"plugin.txt": "x"}).items():
                (target / rel).parent.mkdir(exist_ok=True, parents=True)
                (target / rel).write_text(content)
            return _completed()
        if "rev-parse" in args:
            return _completed(head + "\n")
        if "ls-remote" in args:
            return _completed(f"{head}\tHEAD\n")
        raise AssertionError(args)

    return run, calls


def _location(on_platform=True, host="git.example.com", slug="group/sub/project"):
    return RepoLocation(url=f"https://{host}/{slug}.git", host=host, on_platform=on_platform, slug=slug)


def test_parse_normalises_trailing_slash_dotgit_and_case(client):
    location = parse_repo_url("  HTTPS://Git.Example.COM/Group/Sub/project.git/ ")

    assert location.host == "git.example.com"
    assert location.slug == "Group/Sub/project"
    assert location.url == "https://git.example.com/Group/Sub/project.git"


def test_parse_rejects_http(client):
    with pytest.raises(FetchError):
        parse_repo_url("http://git.example.com/group/project")


def test_parse_rejects_ssh_form(client):
    with pytest.raises(FetchError, match="Use the repository's https link."):
        parse_repo_url("git@git.example.com:group/project.git")


@pytest.mark.parametrize("raw", ["https://user:pw@git.example.com/group/project", "https://tok@git.example.com/a/b"])
def test_parse_rejects_credentials_in_url(client, raw):
    with pytest.raises(FetchError):
        parse_repo_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "https://git.example.com/group/project/-/tree/main",
        "https://git.example.com/group/project?ref=main",
        "https://git.example.com/group/project#readme",
        "https://git.example.com/project",
        "https://git.example.com/",
        "https://git.example.com/group/../project",
    ],
)
def test_parse_rejects_tree_page_url(client, raw):
    with pytest.raises(FetchError):
        parse_repo_url(raw)


def test_on_platform_detected_by_host(client):
    assert parse_repo_url("https://git.example.com/group/project").on_platform is True
    assert parse_repo_url("https://other.example.org/group/project").on_platform is False


def test_private_ip_host_rejected(client, data_path, public_dns):
    public_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 0)), (2, 1, 6, "", ("10.0.0.5", 0))]
    run, calls = _fake_git()

    with (
        patch("plugins.fetch.subprocess.run", side_effect=run),
        pytest.raises(FetchError, match="This host is not reachable from DAIV."),
    ):
        resolve_head(_location(on_platform=False, host="evil.example.org"))

    assert calls == []


def test_unresolvable_off_platform_host_rejected(client, public_dns):
    public_dns.side_effect = OSError("nxdomain")

    with pytest.raises(FetchError, match="This host is not reachable from DAIV."):
        resolve_head(_location(on_platform=False, host="nowhere.example.org"))


def test_off_platform_git_gets_no_credentials(client):
    run, calls = _fake_git()

    with patch("plugins.fetch.subprocess.run", side_effect=run):
        assert resolve_head(_location(on_platform=False, host="github.com", slug="a/b")) == SHA

    client.get_git_auth_env.assert_not_called()
    client.get_repository.assert_not_called()
    env = calls[0][1]["env"]
    assert not any(key.startswith("GIT_CONFIG_KEY") or key == "GIT_CONFIG_VALUE_0" for key in env)


def test_on_platform_git_gets_auth_env(client):
    auth = GitAuthEnv(
        config_key="http.https://git.example.com/.extraheader", header=SecretStr("Authorization: Basic x")
    )
    client.get_git_auth_env.return_value = auth
    run, calls = _fake_git()

    with patch("plugins.fetch.subprocess.run", side_effect=run):
        resolve_head(_location())

    client.get_repository.assert_called_once_with("group/sub/project")
    env = calls[0][1]["env"]
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Basic x"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_on_platform_without_credential_runs_unauthenticated(client):
    client.get_git_auth_env.return_value = None
    run, calls = _fake_git()

    with patch("plugins.fetch.subprocess.run", side_effect=run):
        resolve_head(_location())

    env = calls[0][1]["env"]
    assert "GIT_CONFIG_VALUE_0" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_on_platform_repository_lookup_failure(client):
    client.get_repository.side_effect = RuntimeError("404")

    with pytest.raises(FetchError, match="DAIV can't read this repository."):
        resolve_head(_location())


def test_git_args_disable_redirects_and_symlinks(client, data_path):
    client.get_git_auth_env.return_value = None
    run, calls = _fake_git()

    with patch("plugins.fetch.subprocess.run", side_effect=run):
        resolve_head(_location())
        materialize(_location())

    assert len(calls) == 3
    for args, kwargs in calls:
        joined = " ".join(args)
        assert "http.followRedirects=false" in joined
        assert "core.symlinks=false" in joined
        assert kwargs["env"]["GIT_ALLOW_PROTOCOL"] == "https"
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["timeout"] == settings.GIT_TIMEOUT_SECONDS
    clone_args = next(args for args, _ in calls if "clone" in args)
    for flag in ("--depth", "--single-branch", "--no-tags"):
        assert flag in clone_args


@pytest.mark.parametrize(
    "error",
    [subprocess.CalledProcessError(128, ["git"], stderr="fatal: secret detail"), subprocess.TimeoutExpired(["git"], 1)],
)
def test_git_failures_become_fetch_error_without_stderr(client, error):
    client.get_git_auth_env.return_value = None

    with patch("plugins.fetch.subprocess.run", side_effect=error), pytest.raises(FetchError) as exc_info:
        resolve_head(_location())

    assert "secret detail" not in str(exc_info.value)


def test_materialize_moves_into_revision_dir_and_drops_git(client, data_path):
    client.get_git_auth_env.return_value = None
    run, _ = _fake_git({"skills/a/SKILL.md": "hi"})
    location = _location()

    with patch("plugins.fetch.subprocess.run", side_effect=run):
        commit, path = materialize(location)

    assert commit == SHA
    assert path == revision_dir(location, SHA)
    assert path == data_path / "revisions" / path.parent.name / SHA
    assert (path / "skills/a/SKILL.md").read_text() == "hi"
    assert not (path / ".git").exists()
    assert list((data_path / "tmp").iterdir()) == []


def test_materialize_existing_revision_reused(client, data_path):
    client.get_git_auth_env.return_value = None
    location = _location()
    existing = revision_dir(location, SHA)
    existing.mkdir(parents=True)
    (existing / "marker").write_text("keep")
    run, _ = _fake_git({"new.txt": "new"})

    with patch("plugins.fetch.subprocess.run", side_effect=run):
        commit, path = materialize(location)

    assert (commit, path) == (SHA, existing)
    assert (existing / "marker").read_text() == "keep"
    assert not (existing / "new.txt").exists()
    assert list((data_path / "tmp").iterdir()) == []


def test_oversized_repo_rejected_and_cleaned(client, data_path, monkeypatch):
    client.get_git_auth_env.return_value = None
    monkeypatch.setattr(settings, "MAX_REPO_BYTES", 10)
    run, _ = _fake_git({"big.bin": "x" * 100})
    location = _location()

    with (
        patch("plugins.fetch.subprocess.run", side_effect=run),
        pytest.raises(FetchError, match="Repository is larger than"),
    ):
        materialize(location)

    assert list((data_path / "tmp").iterdir()) == []
    assert not revision_dir(location, SHA).exists()


def test_materialize_rejects_malformed_head(client, data_path):
    client.get_git_auth_env.return_value = None
    run, _ = _fake_git(head="not-a-sha")

    with patch("plugins.fetch.subprocess.run", side_effect=run), pytest.raises(FetchError):
        materialize(_location())

    assert list((data_path / "tmp").iterdir()) == []


def test_prune_keeps_referenced_and_recent(data_path):
    base = data_path / "revisions" / "abcd"
    referenced, recent, stale = base / ("1" * 40), base / ("2" * 40), base / ("3" * 40)
    for path in (referenced, recent, stale):
        path.mkdir(parents=True)
        (path / "f").write_text("x")
    old = time.time() - timedelta(days=3).total_seconds()
    for path in (referenced, stale):
        os.utime(path, (old, old))

    removed = prune_revisions({referenced}, older_than=timedelta(days=1))

    assert removed == 1
    assert referenced.exists()
    assert recent.exists()
    assert not stale.exists()


def test_prune_without_revisions_dir(data_path):
    assert prune_revisions(set()) == 0
