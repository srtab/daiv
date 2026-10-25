import hashlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess  # noqa: S404
import tempfile
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from codebase.clients.base import RepoClient
from core.constants import GIT_PROXY_CA_ENV

from .conf import settings

logger = logging.getLogger("daiv.plugins")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
INHERITED_ENV = ("PATH", *GIT_PROXY_CA_ENV)
GIT_SAFETY_ARGS = ("-c", "http.followRedirects=false", "-c", "core.symlinks=false")
NOT_REACHABLE = "This host is not reachable from DAIV."


class FetchError(Exception):
    """A failure to fetch a plugin repository, with a message safe to show to the user."""


@dataclass(frozen=True)
class RepoLocation:
    url: str
    host: str
    on_platform: bool
    slug: str


def parse_repo_url(raw: str) -> RepoLocation:
    raw = raw.strip()
    if re.match(r"^[\w.-]+@[\w.-]+:", raw):
        raise FetchError("Use the repository's https link.")

    try:
        parts = urlsplit(raw)
    except ValueError:
        raise FetchError("The repository link is not valid.") from None
    if parts.scheme.lower() != "https":
        raise FetchError("Only https repository links are supported.")
    if "@" in parts.netloc:
        raise FetchError("The repository link must not contain credentials.")
    if parts.query or parts.fragment:
        raise FetchError("Use the repository's plain https link.")
    try:
        port = parts.port
    except ValueError:
        port = -1
    host = (parts.hostname or "").lower()
    if not host or port is not None:
        raise FetchError("The repository link is not valid.")

    path = parts.path.rstrip("/").removesuffix(".git").rstrip("/")
    segments = path.strip("/").split("/")
    if "-" in segments:
        raise FetchError("Use the repository's plain https link, not a page inside it.")
    if len(segments) < 2 or any(segment in ("", ".", "..") for segment in segments):
        raise FetchError("The repository link must look like https://host/group/project.")

    slug = "/".join(segments)
    on_platform = host == RepoClient.create_instance().git_host()
    return RepoLocation(url=f"https://{host}/{slug}.git", host=host, on_platform=on_platform, slug=slug)


def revision_path(url: str, commit: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    return settings.DATA_PATH / "revisions" / digest / commit


def revision_dir(location: RepoLocation, commit: str) -> Path:
    return revision_path(location.url, commit)


def resolve_head(location: RepoLocation) -> str:
    env = _git_env(location)
    output = _git(["ls-remote", location.url, "HEAD"], env)
    first = output.split(maxsplit=1)
    if not first or not SHA_RE.match(first[0]):
        raise FetchError("Could not read the repository's default branch.")
    return first[0]


def materialize(location: RepoLocation) -> tuple[str, Path]:
    env = _git_env(location)
    tmp_root = settings.DATA_PATH / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=tmp_root))
    try:
        clone = ["clone", "--depth", "1", "--single-branch", "--no-tags", "--no-checkout", "--", location.url]
        _git([*clone, str(work)], env)
        if _blob_bytes(work, env) > settings.MAX_REPO_BYTES:
            raise FetchError(f"Repository is larger than {settings.MAX_REPO_BYTES // (1024 * 1024)} MB.")

        _git(["-C", str(work), "checkout", "--force", "--quiet", "HEAD"], env)
        commit = _git(["-C", str(work), "rev-parse", "HEAD"], env).strip()
        if not SHA_RE.match(commit):
            raise FetchError("Could not read the repository's current commit.")

        shutil.rmtree(work / ".git", ignore_errors=True)

        target = revision_dir(location, commit)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                Path(work).rename(target)
            except OSError:
                if not target.exists():
                    raise
        return commit, target
    finally:
        shutil.rmtree(work, ignore_errors=True)


def prune_revisions(keep: set[Path], *, older_than: timedelta = timedelta(days=1)) -> int:
    root = settings.DATA_PATH / "revisions"
    if not root.is_dir():
        return 0

    cutoff = time.time() - older_than.total_seconds()
    removed = 0
    for repo_dir in root.iterdir():
        if not repo_dir.is_dir():
            continue
        for revision in repo_dir.iterdir():
            if revision in keep or revision.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(revision, ignore_errors=True)
            removed += 1
        if not any(repo_dir.iterdir()):
            repo_dir.rmdir()
    return removed


def _git_env(location: RepoLocation) -> dict[str, str]:
    credentials: dict[str, str] = {}
    if location.on_platform:
        credentials = _platform_credentials(location.slug)
    else:
        _ensure_public_host(location.host)

    home = settings.DATA_PATH / "tmp" / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in INHERITED_ENV if key in os.environ}
    env.update(credentials)
    env.update({
        "HOME": str(home),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    })
    return env


def _platform_credentials(slug: str) -> dict[str, str]:
    client = RepoClient.create_instance()
    try:
        repository = client.get_repository(slug)
        if client.repository_visibility(slug) == "public":
            return {}
        auth = client.get_git_auth_env(repository)
    except Exception:
        logger.warning("Could not resolve platform access for %s", slug, exc_info=True)
        raise FetchError("DAIV can't read this repository.") from None
    return auth.as_env() if auth is not None else {}


def _ensure_public_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError, UnicodeError:
        raise FetchError(NOT_REACHABLE) from None
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise FetchError(NOT_REACHABLE)
    for address in addresses:
        try:
            is_global = ipaddress.ip_address(address.split("%", 1)[0]).is_global
        except ValueError:
            is_global = False
        if not is_global:
            raise FetchError(NOT_REACHABLE)


def _git(args: list[str], env: dict[str, str]) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *GIT_SAFETY_ARGS, *args],  # noqa: S607
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=settings.GIT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning("git %s failed: %s", args[0] if args[0] != "-C" else args[2], exc.stderr)
        raise FetchError("DAIV could not fetch this repository.") from None
    except subprocess.TimeoutExpired:
        logger.warning("git %s timed out", args[0])
        raise FetchError("Fetching this repository timed out.") from None
    return result.stdout


def _blob_bytes(work: Path, env: dict[str, str]) -> int:
    total = 0
    for line in _git(["-C", str(work), "ls-tree", "-r", "-l", "HEAD"], env).splitlines():
        meta = line.split("\t", 1)[0].split()
        if len(meta) == 4 and meta[3].isdigit():
            total += int(meta[3])
    return total
