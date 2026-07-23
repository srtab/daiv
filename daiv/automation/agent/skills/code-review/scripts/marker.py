#!/usr/bin/env python3
"""Marker helpers for the code-review skill (delivery mode).

Subcommands:
  anchor       compute the 8-hex anchor for an inline finding
  resolve      resolve a target line to new_line/old_line/line_type/in_diff/anchor via the shared diff file
  build        build a <!-- daiv-cr ... --> marker line (inline | summary | reply)
  compose      write a post-ready note body (marker line + verbatim prose) to a file
  parse-notes  parse MR discussions (from a file path, or stdin); emit dedup state + pending replies

The skill owns prose, severity, and posting via the gitlab tool. This script
owns the parts that must be byte-identical across runs so dedup holds across
reruns.
"""
# ruff: NOQA: T201

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

MARKER_PREFIX = "<!-- daiv-cr "
MARKER_SUFFIX = " -->"
SEPARATOR_RE = re.compile(r"^[\s})\];,.]+$")


def compute_anchor(target: str, next_line: str | None) -> str:
    t = target.strip()
    if len(t) < 16 or SEPARATOR_RE.match(t):
        nxt = (next_line or "").strip()
        anchor_input = f"{t}\n{nxt}" if nxt else t
    else:
        anchor_input = t
    return hashlib.sha256(anchor_input.encode("utf-8")).hexdigest()[:8]


DEFAULT_DIFF_PATH = "/workspace/tmp/review-change.diff"
DEFAULT_BODY_DIR = "/workspace/tmp"
MAX_RESOLVE_MATCHES = 20
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _iter_hunk_lines(diff_text: str, file: str):
    """Yield ``(side, old_line, new_line, content)`` for each hunk body line of ``file``.

    ``side`` is ``"+"`` (added: ``old_line`` None), ``"-"`` (deleted: ``new_line`` None),
    or ``" "`` (context: both set). Splitting is ``\\n``-only to match git/GitLab line
    numbering exactly — ``str.splitlines()`` would break on form feed, NEL, ``\\u2028`` etc.,
    which git treats as ordinary in-line bytes, drifting the numbering out of sync.

    Counters are tracked for EVERY file's hunks (not just the target) so header detection
    stays suppressed while any hunk body is being consumed — a content line like ``+++ x``
    can't be misread as a ``+++ b/...`` file header. Lines are yielded only while inside
    ``file``. The ``\\ No newline at end of file`` sentinel is skipped, not counted.
    """
    in_file = False
    old_left = new_left = 0
    old_ln = new_ln = 0

    for line in diff_text.split("\n"):
        in_hunk = old_left > 0 or new_left > 0
        if not in_hunk and line.startswith("diff --git "):
            in_file = False
        elif not in_hunk and line.startswith("+++ "):
            path = line[4:].split("\t")[0].strip()
            path = path[2:] if path.startswith("b/") else path
            in_file = path == file
        elif not in_hunk and (m := _HUNK_RE.match(line)):
            old_ln, old_left = int(m.group(1)), int(m.group(2) or 1)
            new_ln, new_left = int(m.group(3)), int(m.group(4) or 1)
        elif in_hunk:
            if line.startswith("\\"):  # "\ No newline at end of file" — not counted
                continue
            if line.startswith("+"):
                if in_file:
                    yield "+", None, new_ln, line[1:]
                new_ln += 1
                new_left -= 1
            elif line.startswith("-"):
                if in_file:
                    yield "-", old_ln, None, line[1:]
                old_ln += 1
                old_left -= 1
            else:  # context (" ..." or a bare empty line)
                if in_file:
                    yield " ", old_ln, new_ln, line[1:]
                new_ln += 1
                old_ln += 1
                new_left -= 1
                old_left -= 1


def parse_diff_new_side(diff_text: str, file: str) -> dict[int, tuple[int | None, str]]:
    """Map new-side line numbers of ``file`` to ``(old_line, content)``.

    ``old_line`` is ``None`` for an added line (position takes ``new_line`` only) and the
    old-side number for a context line (position takes both). ``content`` is the line text
    as recorded in the diff; the ``resolve`` command gates on ``stale_lines`` before using
    these positions, so a stale shared diff is rejected rather than misplaced. New-side
    lines absent from the map are not shown in the diff and are not inline-eligible.
    """
    positions: dict[int, tuple[int | None, str]] = {}
    for side, old_ln, new_ln, content in _iter_hunk_lines(diff_text, file):
        if side == "+":
            positions[new_ln] = (None, content)
        elif side == " ":
            positions[new_ln] = (old_ln, content)
    return positions


def snippet_in_deleted_lines(diff_text: str, file: str, snippet: str) -> bool:
    """True if ``snippet`` appears literally on a deleted (old-side) line of ``file``.

    Lets ``resolve`` tell a legitimate pure-deletion target (the snippet was removed by
    the diff → correctly demote to summary) apart from a snippet the caller got wrong
    (absent from both the checkout and the diff's deletions → re-derive it).
    """
    return any(side == "-" and snippet in content for side, _o, _n, content in _iter_hunk_lines(diff_text, file))


def stale_lines(positions: dict[int, tuple[int | None, str]], lines: list[str]) -> list[int]:
    """New-side lines whose diff-recorded content doesn't match the checkout.

    Non-empty means the shared diff predates the current checkout (or the checkout is not
    at ``head_sha``): positions derived from it would misplace comments, so ``resolve``
    refuses to emit any rather than emitting confidently wrong ones.
    """
    return [n for n, (_, content) in sorted(positions.items()) if n > len(lines) or lines[n - 1] != content]


def resolve_matches(lines: list[str], positions: dict[int, tuple[int | None, str]], snippet: str) -> list[dict]:
    """Find every line containing ``snippet`` literally; attach position + anchor."""
    matches: list[dict] = []
    for i, text in enumerate(lines, start=1):
        if snippet not in text:
            continue
        next_nonblank = next((ln for ln in lines[i:] if ln.strip()), None)
        entry = positions.get(i)
        old_line = entry[0] if entry else None
        matches.append({
            "new_line": i,
            "old_line": old_line,
            "line_type": (None if entry is None else "added" if old_line is None else "context"),
            "in_diff": entry is not None,
            "target": text,
            "anchor": compute_anchor(text, next_nonblank),
        })
    return matches


def build_marker(
    kind: str,
    sha: str,
    *,
    archetype: str | None = None,
    file: str | None = None,
    line: int | None = None,
    anchor: str | None = None,
) -> str:
    if kind == "inline":
        missing = [
            k for k, v in {"archetype": archetype, "file": file, "line": line, "anchor": anchor}.items() if v is None
        ]
        if missing:
            raise SystemExit(f"inline marker requires: {', '.join(missing)}")
        payload = {
            "v": 1,
            "kind": "inline",
            "archetype": archetype,
            "file": file,
            "line": line,
            "anchor": anchor,
            "sha": sha,
        }
    elif kind == "summary":
        payload = {"v": 1, "kind": "summary", "sha": sha}
    elif kind == "reply":
        payload = {"v": 1, "kind": "reply", "sha": sha}
    else:
        raise SystemExit(f"unknown kind: {kind}")

    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return f"{MARKER_PREFIX}{body}{MARKER_SUFFIX}"


def parse_marker(body: str) -> dict | None:
    if not body.startswith(MARKER_PREFIX):
        return None
    first_line = body.split("\n", 1)[0]
    if not first_line.endswith(MARKER_SUFFIX):
        return None
    json_str = first_line[len(MARKER_PREFIX) : -len(MARKER_SUFFIX)]
    try:
        payload = json.loads(json_str)
    except json.JSONDecodeError as exc:
        # Prefix matched but JSON is malformed — surface the corruption rather
        # than silently dropping the marker; otherwise the same finding would
        # be re-posted on the next review (no fingerprint match against a
        # corrupted prior note).
        sys.stderr.write(f"warning: corrupt daiv-cr marker, skipping: {exc}\n")
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    return payload


def parse_notes(discussions: list[dict]) -> dict:
    inline: list[list] = []
    summary: dict | None = None
    last_reviewed_sha: str | None = None
    pending_replies: list[dict] = []

    for disc in discussions:
        notes = disc.get("notes") or []
        if not notes:
            continue

        seed_idx = None
        seed_payload = None
        for i, note in enumerate(notes):
            payload = parse_marker(note.get("body", ""))
            if payload is not None and payload.get("kind") in ("inline", "summary"):
                seed_idx = i
                seed_payload = payload
                break

        if seed_payload is None:
            continue

        seed_kind = seed_payload["kind"]

        if seed_kind == "inline":
            if all(seed_payload.get(k) for k in ("archetype", "file", "anchor")):
                inline.append(["inline", seed_payload["archetype"], seed_payload["file"], seed_payload["anchor"]])
        elif seed_kind == "summary" and summary is None:
            # `body` lets the delivery step carry forward prior discussion-only findings on a
            # delta re-review (gitlab-delivery.md Step 6) straight from this structured output —
            # the raw discussion JSON is written to a file and never enters the agent's context.
            summary = {
                "discussion_id": disc.get("id"),
                "note_id": notes[seed_idx].get("id"),
                "body": notes[seed_idx].get("body", ""),
            }
            # The summary marker's sha is the head at the previous review — the detection
            # base for a delta re-review (review-workflow.md, scope stage).
            last_reviewed_sha = seed_payload.get("sha")

        last_daiv_idx = seed_idx
        for i in range(seed_idx + 1, len(notes)):
            if parse_marker(notes[i].get("body", "")) is not None:
                last_daiv_idx = i

        resolved = any(n.get("resolved") for n in notes)
        if not resolved and last_daiv_idx < len(notes) - 1:
            pending_replies.append({
                "kind": seed_kind,
                "discussion_id": disc.get("id"),
                "notes": [
                    {
                        "author": (n.get("author") or {}).get("username"),
                        "body": n.get("body", ""),
                        "system": bool(n.get("system")),
                    }
                    for n in notes
                ],
            })

    return {
        "inline_fingerprints": inline,
        "summary": summary,
        "last_reviewed_sha": last_reviewed_sha,
        "pending_replies": pending_replies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_anchor = sub.add_parser("anchor", help="Compute the 8-hex anchor for an inline finding.")
    p_anchor.add_argument("--target", required=True, help="Target line content (new-side: added or context).")
    p_anchor.add_argument(
        "--next",
        dest="next_line",
        default=None,
        help="Next non-blank new-side line. Used only when the target line is short or all-separators.",
    )

    p_resolve = sub.add_parser(
        "resolve", help="Resolve a target line: new_line/old_line/line_type/in_diff/anchor from the file + shared diff."
    )
    p_resolve.add_argument("--file", required=True, help="new_path, relative to the repo root (the CWD).")
    p_resolve.add_argument("--snippet", required=True, help="Literal (non-regex) snippet of the target line.")
    p_resolve.add_argument(
        "--diff",
        default=DEFAULT_DIFF_PATH,
        help=f"Path to the shared unified diff file (default: {DEFAULT_DIFF_PATH}).",
    )

    p_build = sub.add_parser("build", help="Build a <!-- daiv-cr ... --> marker line.")
    p_build.add_argument("--kind", choices=["inline", "summary", "reply"], required=True)
    p_build.add_argument("--sha", required=True, help="head_sha at posting time.")
    p_build.add_argument("--archetype", help="Inline only: archetype name.")
    p_build.add_argument("--file", help="Inline only: new_path from the diff.")
    p_build.add_argument("--line", type=int, help="Inline only: new_line from the diff.")
    p_build.add_argument("--anchor", help="Inline only: 8-hex anchor from `marker.py anchor`.")

    p_compose = sub.add_parser(
        "compose", help="Write a post-ready note body (marker line + verbatim prose from --prose-file) to a file."
    )
    p_compose.add_argument("--kind", choices=["inline", "summary", "reply"], required=True)
    p_compose.add_argument("--sha", required=True, help="head_sha at posting time.")
    p_compose.add_argument("--archetype", help="Inline only: archetype name.")
    p_compose.add_argument("--file", help="Inline only: new_path from the diff.")
    p_compose.add_argument("--line", type=int, help="Inline only: new_line from the diff.")
    p_compose.add_argument("--anchor", help="Inline only: 8-hex anchor from `marker.py anchor`.")
    p_compose.add_argument(
        "--prose-file",
        dest="prose_file",
        required=True,
        help="Path to the note prose (everything after the marker line). Read verbatim, byte-for-byte.",
    )
    p_compose.add_argument(
        "--out",
        default=None,
        help=f"Where to write the composed body. Default: {DEFAULT_BODY_DIR}/cr-body-<hash8>.md, where "
        "hash8 is the first 8 hex of the composed body's SHA-256 (content-derived so a stateless rerun "
        "cannot collide with or silently reuse a stale body file).",
    )

    p_parse = sub.add_parser(
        "parse-notes",
        help="Parse existing MR discussions (JSON array) from a file path, or stdin. Emit dedup state on stdout.",
    )
    p_parse.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Path to a JSON file holding the discussion array (e.g. the gitlab tool's output_to_file "
        "dump). Omit to read the array from stdin.",
    )

    args = parser.parse_args()

    if args.cmd == "anchor":
        print(compute_anchor(args.target, args.next_line))
        return 0

    if args.cmd == "resolve":
        if not args.snippet.strip():
            sys.stderr.write("empty snippet: pass a distinctive literal run of the target line\n")
            return 1
        file_path = Path(args.file)
        if not file_path.is_file():
            sys.stderr.write(f"file not found: {args.file} (run from the repo root, checked out at head_sha)\n")
            return 1
        diff_path = Path(args.diff)
        if not diff_path.is_file():
            sys.stderr.write(
                f"diff file not found: {args.diff} — regenerate it with "
                "`git diff <target>...<source> > <path>` and retry\n"
            )
            return 1
        lines = file_path.read_text(encoding="utf-8", errors="replace").split("\n")
        diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
        positions = parse_diff_new_side(diff_text, args.file)
        if stale := stale_lines(positions, lines):
            sys.stderr.write(
                f"stale diff: {len(stale)} new-side line(s) of {args.file} (first: line {stale[0]}) don't match "
                "the checkout — the shared diff predates it (or the checkout isn't at head_sha); regenerate it "
                "with `git diff <target>...<source> > <path>` and retry\n"
            )
            return 1
        matches = resolve_matches(lines, positions, args.snippet)
        if len(matches) > MAX_RESOLVE_MATCHES:
            sys.stderr.write(
                f"snippet too common ({len(matches)} matches in {args.file}); "
                "use a longer, more distinctive run of the target line\n"
            )
            return 1
        out: dict = {"file": args.file, "matches": matches}
        if not matches:
            # No new-side match. Distinguish a genuine pure deletion (snippet on a deleted
            # line → correctly demote to summary) from a wrong snippet (absent from the diff
            # entirely → the caller should re-derive it before demoting).
            out["snippet_in_deletion"] = snippet_in_deleted_lines(diff_text, args.file, args.snippet)
        json.dump(out, sys.stdout)
        sys.stdout.write("\n")
        return 0

    if args.cmd == "build":
        print(
            build_marker(
                args.kind, args.sha, archetype=args.archetype, file=args.file, line=args.line, anchor=args.anchor
            )
        )
        return 0

    if args.cmd == "compose":
        prose_path = Path(args.prose_file)
        if not prose_path.is_file():
            sys.stderr.write(f"prose file not found: {args.prose_file}\n")
            return 1
        # `build_marker` owns the (double-quoted, byte-stable) first line; SystemExit on a
        # missing inline field propagates exactly as `build` does.
        marker_line = build_marker(
            args.kind, args.sha, archetype=args.archetype, file=args.file, line=args.line, anchor=args.anchor
        )
        # Prose is read as raw bytes and appended verbatim — the caller's text is never JSON
        # re-encoded, so a `suggestion` block (backticks, quotes, newlines) survives intact.
        prose_bytes = prose_path.read_bytes()
        body_bytes = marker_line.encode("utf-8") + b"\n" + prose_bytes
        if args.out:
            out_path = Path(args.out)
        else:
            # Content-derived name: a stateless rerun with the same body reuses the same file, and
            # a different body can never clobber it (no counter state to track `<n>`).
            hash8 = hashlib.sha256(body_bytes).hexdigest()[:8]
            out_path = Path(DEFAULT_BODY_DIR) / f"cr-body-{hash8}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(body_bytes)
        print(str(out_path))
        return 0

    if args.cmd == "parse-notes":
        try:
            if args.path:
                with Path(args.path).open(encoding="utf-8") as fh:
                    discussions = json.load(fh)
            else:
                discussions = json.load(sys.stdin)
        except FileNotFoundError:
            sys.stderr.write(f"file not found: {args.path}\n")
            return 1
        except OSError as exc:
            sys.stderr.write(f"cannot read {args.path}: {exc}\n")
            return 1
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # A non-UTF-8 dump (UnicodeDecodeError) is the same class of failure as malformed
            # JSON: the file isn't parseable discussion data. Both are ValueError subclasses,
            # disjoint from the OSError hierarchy above, so order against it is irrelevant.
            sys.stderr.write(f"invalid JSON in {args.path or 'stdin'}: {exc}\n")
            return 1
        if not isinstance(discussions, list):
            sys.stderr.write(f"expected a JSON array of discussions in {args.path or 'stdin'}\n")
            return 1
        json.dump(parse_notes(discussions), sys.stdout)
        sys.stdout.write("\n")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
