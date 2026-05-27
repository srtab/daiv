#!/usr/bin/env python3
"""Marker helpers for the code-review skill (delivery mode).

Subcommands:
  anchor       compute the 8-hex anchor for an inline finding
  build        build a <!-- daiv-cr ... --> marker line (inline | summary | reply)
  parse-notes  parse MR discussions on stdin; emit dedup state + pending replies

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
            "line": int(line),
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
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    return payload


def parse_notes(discussions: list[dict]) -> dict:
    inline: list[list] = []
    summary: dict | None = None
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
            summary = {"discussion_id": disc.get("id"), "note_id": notes[seed_idx].get("id")}

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

    return {"inline_fingerprints": inline, "summary": summary, "pending_replies": pending_replies}


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

    p_build = sub.add_parser("build", help="Build a <!-- daiv-cr ... --> marker line.")
    p_build.add_argument("--kind", choices=["inline", "summary", "reply"], required=True)
    p_build.add_argument("--sha", required=True, help="head_sha at posting time.")
    p_build.add_argument("--archetype", help="Inline only: archetype name.")
    p_build.add_argument("--file", help="Inline only: new_path from the diff.")
    p_build.add_argument("--line", type=int, help="Inline only: new_line from the diff.")
    p_build.add_argument("--anchor", help="Inline only: 8-hex anchor from `marker.py anchor`.")

    sub.add_parser(
        "parse-notes", help="Parse existing MR discussions on stdin (JSON array). Emit dedup state on stdout."
    )

    args = parser.parse_args()

    if args.cmd == "anchor":
        print(compute_anchor(args.target, args.next_line))
        return 0

    if args.cmd == "build":
        print(
            build_marker(
                args.kind, args.sha, archetype=args.archetype, file=args.file, line=args.line, anchor=args.anchor
            )
        )
        return 0

    if args.cmd == "parse-notes":
        try:
            discussions = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"invalid JSON on stdin: {exc}\n")
            return 1
        if not isinstance(discussions, list):
            sys.stderr.write("expected a JSON array of discussions on stdin\n")
            return 1
        json.dump(parse_notes(discussions), sys.stdout)
        sys.stdout.write("\n")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
