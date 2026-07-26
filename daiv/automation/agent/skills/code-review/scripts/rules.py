#!/usr/bin/env python3
"""Trusted rule-source snapshots for the code-review skill (Stage 0).

Subcommand:
  snapshot   materialize each rule source's base-revision content into a scratch dir and
             print a JSON manifest

Custom review rules must come from the review's immutable base revision, never the PR's
working tree: a rule file the PR itself adds or edits must not govern its own review, or a
diff could grant itself authority over how it is reviewed. ``git show <base_sha>:<path>`` is
that immutable read, and this script — not the model — writes the bytes, so nothing about the
rules depends on transcription.
"""
# ruff: NOQA: T201

import argparse
import json
import os
import re
import subprocess  # noqa: S404
import sys
from pathlib import Path

# A base revision must be a full object id. A symbolic or abbreviated revision ("HEAD", "main",
# a short sha) is mutable or ambiguous, and on a PR branch it resolves to a commit that CONTAINS
# the PR's rule edits — which would hand a diff authority over its own review.
_FULL_OID = re.compile(r"\A[0-9a-fA-F]{40}\Z")

# git can block indefinitely on a credential prompt (partial clone fetching a missing blob) or a
# stalled fsmonitor/NFS mount. A hang is the least debuggable failure shape, so cap it.
_GIT_TIMEOUT_SECONDS = 30

# Logical rule-source paths in precedence order. The flag marks the authoritative (binding)
# source: `.agents/review-rules.md` wins when concrete rules conflict (agents/cr-custom-rules.md).
RULE_SOURCES: tuple[tuple[str, bool], ...] = (
    (".agents/review-rules.md", True),
    ("AGENTS.md", False),
    (".agents/AGENTS.md", False),
)

DEFAULT_SNAPSHOT_DIR = "/workspace/tmp/code-review-rules"

# Everything outside this set collapses to "_", so a sanitized name can never contain a path
# separator and therefore can never escape the snapshot dir. A ".." sequence survives only as
# literal filename bytes, which is harmless.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def snapshot_filename(logical_path: str) -> str:
    """Flatten a repository path into one safe filename."""
    name = _UNSAFE.sub("_", logical_path.strip("/"))
    # "." and ".." pass the character filter (both chars are safe) but are directory references,
    # not names: "<dir>/.." resolves to the PARENT of the snapshot dir. Neutralize them.
    if name in {"", ".", ".."}:
        return "_"
    return name


def snapshot_path(snapshot_dir: str, logical_path: str) -> Path:
    """Resolve a logical path to its snapshot location, refusing to escape ``snapshot_dir``.

    Belt-and-braces: ``snapshot_filename`` already guarantees a separator-free, non-relative
    name, so this can only fire if that guarantee is ever weakened.
    """
    root = Path(snapshot_dir)
    candidate = root / snapshot_filename(logical_path)
    if candidate.parent != root or candidate.name in {".", ".."}:
        raise ValueError(f"refusing to write outside {snapshot_dir}: {candidate}")
    return candidate


def write_snapshot(target: Path, content: bytes) -> None:
    """Write ``content`` to ``target``, refusing to follow a symlink already sitting there.

    ``snapshot_path`` constrains the path STRING, which says nothing about what is on disk at
    that name. The snapshot dir is a fixed, predictable location in a shared sandbox, so a
    symlink planted at the target would make ``write_bytes`` write THROUGH it to an arbitrary
    file while the manifest reported a clean snapshot. Unlink first, then create with
    ``O_NOFOLLOW | O_EXCL`` so a link re-planted in between fails the write instead of
    redirecting it.
    """
    target.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(target, flags, 0o600)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)


def _git(repo: str, *args: str) -> tuple[int, bytes, str]:
    """Run git in ``repo``, returning ``(returncode, stdout_bytes, stderr_text)``.

    stdout stays bytes: a rule file is copied through verbatim, never decoded and re-encoded.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", repo, *args],  # noqa: S607
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            # Never block on a credential prompt: a partial clone that cannot fetch a blob must
            # fail fast into `degraded`, not stall Stage 0 forever waiting on stdin.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        return 124, b"", f"git timed out after {_GIT_TIMEOUT_SECONDS}s"
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace").strip()


def _list_rule_sources(repo: str, base_sha: str) -> tuple[set[str], str | None]:
    """Which rule sources exist at ``base_sha``, or the reason the base revision is unusable.

    One `ls-tree` over every source answers both questions: `^{commit}` makes it resolve the
    revision (non-zero on a base that is unknown or not a commit) and its output lists exactly
    the paths present there, so a missing path and a failed read stay structurally distinct.
    `cat-file -e` cannot make that distinction — it exits 128 for a missing path AND for an
    unreadable object store — so probing per path with it would file a partial-clone or
    permissions failure as "no rules exist". `-z` keeps paths raw, so git never quotes an
    unusual filename.
    """
    if not _FULL_OID.match(base_sha):
        # Refuse a mutable revision rather than resolving it: on a PR branch "HEAD"/"main" name a
        # commit that already contains the PR's rule edits, which is exactly the self-governance
        # this script exists to prevent. Degrade (never a silent clean skip) so the review still
        # runs and reports that custom-rule coverage was lost.
        return set(), f"base revision {base_sha!r} is not a full 40-character object id"

    returncode, listing, stderr = _git(
        repo, "ls-tree", "-z", "--name-only", f"{base_sha}^{{commit}}", "--", *(path for path, _ in RULE_SOURCES)
    )
    if returncode != 0:
        # An unresolvable base revision is NOT "this repo has no rules": every source degrades so
        # the review reports reduced coverage instead of a silent clean skip.
        return set(), stderr or f"base revision {base_sha} is not available in this clone"
    return {entry for entry in listing.decode("utf-8", "replace").split("\0") if entry}, None


def _notes(manifest: dict, repo: str) -> list[str]:
    """Plain-language obligations for the status line — same idiom as findings.py status_notes.

    Every note is something the run must surface, so an empty list means unremarkable Stage 0.
    """
    notes: list[str] = []
    base_sha = manifest["base_sha"]
    for entry in manifest["degraded"]:
        notes.append(
            f"could not read {entry['path']} at base revision {base_sha} ({entry['error']}) — custom-rule "
            "coverage is degraded; report it in the status line and never fall back to the working-tree copy."
        )
    for path in manifest["absent"]:
        # Existence probe only — never the file's CONTENT. The working tree is untrusted for rule
        # text; "does this path exist now" is safe metadata. State the observation rather than
        # inferring "the PR added it": the file may also have landed on the target branch after
        # this PR branched, or be untracked scratch.
        if (Path(repo) / path).exists():
            notes.append(
                f"{path} is present in the working tree but not at base revision {base_sha}, so it does not "
                "govern this review; it is reviewed as ordinary diff content and takes effect after merge."
            )
    if not manifest["sources"] and not manifest["degraded"]:
        notes.append(
            "No rule source existed at the base revision — skip cr-custom-rules. This is a clean skip, "
            "not a degraded review."
        )
    return notes


def snapshot(repo: str, base_sha: str, snapshot_dir: str) -> dict:
    """Materialize every rule source's ``base_sha`` content into ``snapshot_dir``.

    Returns a manifest with ``sources`` (governing snapshots), ``absent`` (not present at the
    base revision — including anything this PR adds), ``degraded`` (present but unreadable),
    ``notes``, and ``dispatch_custom_rules`` — the single gate Stage 0 reads to decide whether
    ``cr-custom-rules`` runs at all.
    """
    manifest: dict = {"base_sha": base_sha, "snapshot_dir": snapshot_dir, "sources": [], "absent": [], "degraded": []}

    present, base_error = _list_rule_sources(repo, base_sha)
    if base_error:
        manifest["degraded"] = [{"path": path, "error": base_error} for path, _ in RULE_SOURCES]
    else:
        for logical_path, authoritative in RULE_SOURCES:
            if logical_path not in present:
                manifest["absent"].append(logical_path)
                continue
            returncode, content, stderr = _git(repo, "show", f"{base_sha}:{logical_path}")
            if returncode != 0:
                manifest["degraded"].append({"path": logical_path, "error": stderr or "git show failed"})
                continue
            try:
                target = snapshot_path(snapshot_dir, logical_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                write_snapshot(target, content)
            except (OSError, ValueError) as exc:
                manifest["degraded"].append({"path": logical_path, "error": str(exc)})
                continue
            manifest["sources"].append({"path": logical_path, "snapshot": str(target), "authoritative": authoritative})

    manifest["notes"] = _notes(manifest, repo)
    manifest["dispatch_custom_rules"] = bool(manifest["sources"])
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    snapshot_parser = sub.add_parser("snapshot", help="Materialize base-revision rule sources into a scratch dir.")
    snapshot_parser.add_argument(
        "--base-sha",
        required=True,
        help="The review's base revision, as a full 40-character object id. Symbolic revisions "
        "(HEAD, main) and short shas are refused: they are mutable, and on a PR branch they "
        "resolve to a commit containing the PR's own rule edits.",
    )
    snapshot_parser.add_argument("--repo", default=".", help="Repository working directory (default: cwd).")
    snapshot_parser.add_argument(
        "--snapshot-dir",
        default=DEFAULT_SNAPSHOT_DIR,
        help=f"Where snapshots are written (default: {DEFAULT_SNAPSHOT_DIR}).",
    )
    args = parser.parse_args()

    if args.cmd == "snapshot":
        try:
            manifest = snapshot(args.repo, args.base_sha, args.snapshot_dir)
        except OSError as exc:
            # Only reachable when git itself is unavailable — a per-source read or write failure
            # is captured as `degraded` inside snapshot() instead of aborting.
            sys.stderr.write(f"could not produce the rule snapshot manifest: {exc}\n")
            return 1
        json.dump(manifest, sys.stdout)
        sys.stdout.write("\n")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
