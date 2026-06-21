# Marker format reference

Every note daiv posts begins with a single-line HTML comment carrying a JSON payload:

```
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"...","file":"...","line":42,"anchor":"a1b2c3d4","sha":"abc1234"} -->
```

`scripts/marker.py` is the canonical implementation — never compute anchors or assemble markers by hand. This file explains what each field means and why; the script's `anchor`, `build`, and `parse-notes` subcommands are the source of truth for *how*. This reference covers only the marker *payload* fields; the structure `parse-notes` *emits* (the dedup state — `inline_fingerprints`, `summary` including the prior summary's `body` used for Step 6 carry-forward, and `pending_replies`) is documented in `gitlab-delivery.md` Step 1.

## Fields

- `v` — marker schema version (currently `1`). Markers with an unknown `v` are ignored entirely — `parse-notes` drops them, so they don't dedup against current findings and they don't surface as `pending_replies` either.
- `kind` — `inline`, `summary`, or `reply`. Reply markers carry only `v`, `kind`, and `sha` — no archetype/file/line/anchor, since replies inherit their thread from the discussion they're posted to. They exist so daiv-authored detection stays uniform (one prefix rule for findings and replies, no author-username lookup).
- `archetype` — inline-eligible archetype name (inline only).
- `file` — `new_path` from the diff (inline only).
- `line` — `new_line` from the diff (inline only). **Diagnostic only — not used in dedup**, because line numbers shift on unrelated commits.
- `anchor` — stable 8-hex identity for inline findings, computed as the first 8 hex chars of `sha256` over the stripped target line (with a disambiguator that appends the next non-blank new-side line **when the target is under 16 chars or all-separators**). Only the line content feeds the anchor — diagnostic fields don't. **Known collision:** because only line *content* feeds the hash, two byte-identical target lines (≥16 chars, not all-separators) in different hunks of the same file produce the **same** anchor, so a `(kind, archetype, file, anchor)` fingerprint cannot tell two distinct findings on them apart. This is the deliberate trade for cross-commit stability (the anchor ignores line numbers, which shift on unrelated commits). It is not fixed in the hash because the algorithm is `v:1` and byte-locked: widening the input (e.g. always folding in neighbouring lines) would re-fingerprint every already-posted marker and re-post every prior finding, so a real fix must ride a marker `v` bump. Until then, delivery handles it at posting time — `gitlab-delivery.md` Step 4 demotes the second within-run colliding finding to the summary rather than dropping it.
- `sha` — `head_sha` at posting time (all kinds). Diagnostic only.

## Dedup fingerprint

- Inline: `(kind, archetype, file, anchor)`.
- Summary: `kind=summary` — exactly one summary daiv note may exist per MR.

## Daiv-authored detection

A note is treated as daiv-posted **iff** its body begins with the literal prefix `<!-- daiv-cr ` followed by a parseable JSON payload terminating in ` -->`. Author username is *not* used. `parse-notes` applies this rule; do not reimplement it.

## Resolution semantics

A discussion's `resolved` state does not affect dedup. If the user resolves a thread without applying the suggestion (or with any other outcome), the marker stays on the resolved note, so `parse-notes` still surfaces its fingerprint on the next review and the same finding is skipped. Resolution is a UX signal between humans, not an instruction to forget. The one thing `resolved` *does* affect is reply handling (Step 2): resolved threads are dropped from `pending_replies` since the conversation is closed.
