# GitLab Delivery

Delivery mode only. You reach here after `references/review-workflow.md` has produced verified findings — deliver them as inline discussions on specific lines plus one top-level summary, posted via the `gitlab` tool. The review is done; you're publishing it, not re-adjudicating it.

Relative paths (`scripts/…`, `references/…`, `examples/…`) resolve under the skill root injected in `SKILL.md`.

## Inputs (from the review workflow)

Carry these in from the verified-findings handoff:

- **scope** + the SHA triplet (`base_sha`, `start_sha`, `head_sha`). Markers and positions use `head_sha`.
- the **verified findings**, each with `detector`, `file`, `line`, `bar`, `archetype`, `title`, `rationale`, optional `suggestion`, and **severity**.
- **detector status** (dispatched/expected, or `inline (triage)` when Stage 1 skipped fan-out) and **merge stats** (`candidates`/`dropped`/`merged`) for Step 7's status line.

**Even with zero verified findings, do not skip delivery.** Steps 1, 2, and 6 still run — parse notes, address replies, reconcile the summary (update in place; write nothing if none exists). Only Steps 3–5 are skipped.

## Marker format

`scripts/marker.py` is the canonical implementation of the marker contract — **never compute anchors, positions, or markers by hand.** Its `resolve`, `anchor`, `build`, and `parse-notes` subcommands are deterministic and version-stable. Run `scripts/marker.py <cmd> --help` for arguments. Field meanings, daiv-authored detection, and resolution semantics live in `references/marker-format.md`.

This procedure decides only two things; the script does the rest:

- **Anchor target — which line.** Inline findings anchor on **added or context** lines on the new side of the diff. Pure-deletion findings (no new-side line) demote to the summary. For a multi-line finding (`suggestion:-N+M`, or a question over a contiguous block), the target is the **first** new-side line of the range. The model picks the line; the script computes everything else.
- **Dedup fingerprint — what to compare.** Inline: `(kind, archetype, file, anchor)`; summary: `kind=summary` (exactly one summary daiv note per MR). Step 4 builds these.

## Step 1 — Acquire context and dedup state

- Read `merge_request_id`, project, and the SHA triplet from the runtime context; if any is missing, demote to interactive mode and return markdown.
- **List existing discussions to a file, then parse that file** — never transcribe the JSON into context. Call the `gitlab` tool with subcommand `project-merge-request-discussion list --mr-iid <iid> --get-all` and `output_to_file=true`:
  - `--get-all` is **mandatory** — without it only the first page loads and later-page findings get reposted.
  - `output_to_file=true` forces `--output json` (`parse-notes`' only readable form), writes the full array to a file, and returns just its absolute path — the blob never enters context. In a separate `bash` call, run `python3 scripts/marker.py parse-notes <path>` (it also reads stdin; pass the path here).
  - **Empty listing:** no file written means no existing discussions — treat `inline_fingerprints`, `summary`, and `pending_replies` as empty; skip `parse-notes`.

  `parse-notes` returns:
  ```json
  {"inline_fingerprints": [["inline", "<archetype>", "<file>", "<anchor>"], ...],
   "summary": {"discussion_id": "...", "note_id": ..., "body": "<prior summary markdown>"} | null,
   "pending_replies": [
     {"kind": "inline"|"summary",
      "discussion_id": "...",
      "notes": [{"author": "<username>", "body": "<text>", "system": false}, ...]},
     ...
   ]}
  ```
  Keep all three: `inline_fingerprints` is Step 4's **dedup set**; `summary` tells Step 6 whether to update or create fresh, **and carries the prior body** for its carry-forward rule; `pending_replies` lists unresolved daiv threads (Step 2).

## Step 2 — Address pending replies

For each discussion in `pending_replies`, decide the outcome from its conversation. Every thread is already open — `parse-notes` excludes resolved threads, so never re-check `resolved`. Skip threads whose only notes after daiv's last are `system: true` (e.g. a label change).

| Outcome | Action |
|---|---|
| **Daiv concedes** — false positive or no longer applies | Acknowledge briefly, then **resolve the thread** (command below). |
| **User concedes** — accepts it, or already pushed a fixing commit | Acknowledge briefly. **Do not resolve** — the user resolves when they apply the fix. |
| **Disagreement persists** — user pushes back, daiv still holds | Defend with new evidence or a concrete restatement. **Do not resolve.** |

Daiv resolves only when it withdraws; posting without resolving is the default. Resolving doesn't leak a repost — the marker stays on the resolved note, keeping its fingerprint in the dedup set.

Build each reply's marker with `scripts/marker.py build --kind reply --sha <head_sha>` and place its output as the reply body's first physical line.

Post the reply:

```
gitlab project-merge-request-discussion-note create --mr-iid <iid> --discussion-id <discussion_id> --body "<reply body>"
```

Resolve the thread (only in the *Daiv concedes* case):

```
gitlab project-merge-request-discussion update --mr-iid <iid> --id <discussion_id> --resolved true
```

The `--id` flag differs by subcommand: **discussion** id on `discussion update`, **note** id on `discussion-note update`. Reply create instead takes `--discussion-id`.

Keep replies short. If there is nothing concrete to add beyond "noted", post nothing and leave the thread for the user.

After addressing pending replies, continue to bucketing new findings — a reply isn't a dedup entry; the original fingerprint already prevents reposts.

## Step 3 — Bucket findings

Inline delivery has two shapes, anchoring on a single new-side line (or contiguous range) and deduping per the fingerprint rule above.

**Fix archetypes** — a concrete code change as a `suggestion` block replacing a contiguous new-side range in one hunk: `remove_dead_lines`, `use_framework_idiom`, `replace_with_constant`, `swap_library_call`. The range may span several lines (`suggestion:-N+M`); unchanged lines inside it are restated verbatim.

**Question archetype** — `question`: a targeted, single-line/contiguous-range yes/no question — marker + 1-2 sentences ending in `?`, no `suggestion` block. Use it for findings meeting the Signal filter's *Question* bar, landing in-diff rather than buried in the summary. Same range/anchor rule as fix archetypes; pure-deletion questions demote to summary.

Demote to **discussion-only** when no clean inline shape exists: multi-file/non-adjacent-hunk findings, prose-only conclusions (e.g., "needs restructuring"), un-anchorable questions, rename-impact questions, or renames themselves — a `suggestion` patches only the declaration, not call sites (see `references/few-shot-examples.md` Part 2).

If an inline finding's diff position can't be constructed reliably (file renamed, line moved within a hunk, ambiguous anchor), **demote it to discussion-only**.

## Step 4 — Resolve the line, then apply dedup

**Resolve every candidate's position with `scripts/marker.py resolve` — never by counting hunk lines or hand-computing anchors.** From the repo root (checked out at `head_sha`):

```
python3 scripts/marker.py resolve --file <new_path> --snippet "<distinctive literal run of the target line>" --diff /workspace/tmp/review-change.diff
```

The snippet is matched literally (no regex escaping needed). The output lists every matching line with its position and anchor:

```json
{"file": "<new_path>", "matches": [{"new_line": 42, "old_line": null, "line_type": "added", "in_diff": true, "target": "<full line>", "anchor": "a1b2c3d4"}]}
```

- **Pick the match inside the finding's hunk** with `in_diff: true`. `in_diff: false` means the line isn't shown in the diff — not inline-eligible; demote to summary.
- **No matches** → the target is a pure deletion; demote to summary.
- **`line_type: "context"`** → the GitLab position needs both the returned `old_line` and `new_line`. **`"added"`** → `new_line` only.
- The returned `anchor` is final — no separate `anchor` call.
- If `resolve` exits 1 reporting the shared diff file **missing or stale** (stale = it no longer matches the checkout — e.g. written before a new push, or left over from a triage-path run that never rewrote it), regenerate it (`git diff <target>...<source> > /workspace/tmp/review-change.diff`) and retry; if it still fails or the match is ambiguous (e.g. file renamed across the diff), demote to discussion-only. Never post a misaligned suggestion or a misanchored question.

Form the fingerprint `["inline", archetype, file, anchor]` and compare:

- **Collision with the dedup set (a prior run posted it):** skip — do not rephrase, do not "post a stronger version."
- **Collision with a fingerprint you already posted *this run*:** demote the second finding to discussion-only (Step 6) — byte-identical target lines in different hunks share one anchor (`references/marker-format.md` documents the trade-off), and the second finding is real; silently dropping it would lose it.

## Step 5 — Post inline findings

For each surviving inline finding:

1. Build the marker line with `scripts/marker.py build --kind inline --sha <head_sha> --archetype <X> --file <new_path> --line <new_line> --anchor <anchor>` — `<new_line>` is the `new_line` from Step 4's resolve output. Capture verbatim as the note body's first line.
2. Post via `gitlab project-merge-request-discussion create` with `--position`, using that same `new_line` (plus `old_line` when Step 4 returned `line_type: "context"`) — follow the `gitlab` tool's guidance; don't invent your own.

Body shape depends on the archetype:

- **Fix archetype** (the four archetypes from Step 3): marker line, a short comment (1-2 sentences), then a `suggestion` block replacing the target range — the suggestion IS the value.
- **Question archetype** (`question`): marker line, then 1-2 sentences ending in `?`, no `suggestion` block — the question IS the value.

Keep bodies tight — prose is justification, not filler. Example marker lines:

```
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"remove_dead_lines","file":"services/api.py","line":42,"anchor":"a1b2c3d4","sha":"abc1234"} -->
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"question","file":"env_files/all/grafana.env","line":9,"anchor":"b2c3d4e5","sha":"abc1234"} -->
```

## Step 6 — Post or update the summary discussion

Compose **one** top-level summary containing:

- All discussion-only findings, grouped by severity (High/Medium/Low) — same shape as the interactive Findings list: one-line summary + `<details>` block.
- A short **Questions** section for discussion-only questions (cross-file or un-anchored) — targeted questions go inline (Step 3), not here.
- A one-line index of the inline findings posted this run (file + line + archetype) so reviewers see the full picture without expanding diffs.

**Delta-only re-reviews must not shrink the summary.** A discussion-only finding has no inline fingerprint — the prior summary's `body` (Step 1) is its only record. When this run re-examined only a delta, re-read that body and **carry forward every discussion-only finding whose subject lies outside the delta**: it was neither confirmed nor disproved, so it stays. Drop a prior finding only when this run actively disproves it or reposts it inline (the inline copy supersedes the prose). The body you post is the **union** of carried-forward prior findings and this run's — never this run's alone. (On a full re-review nothing lies outside the delta, so the union reduces to this run's findings.)

Build the marker with `scripts/marker.py build --kind summary --sha <head_sha>` and place its output as the body's first physical line. Example:

```
<!-- daiv-cr {"v":1,"kind":"summary","sha":"abc1234"} -->
```

If `summary` from Step 1 was non-null, **update the existing note in place** (`gitlab project-merge-request-discussion-note update --mr-iid <iid> --discussion-id <discussion_id> --id <note_id> --body "..."`) — here `--id` is the **note** id (`summary.note_id`), not the discussion id (Step 2). The summary always reflects current state (this run's plus carried-forward) — one discussion per MR, never a second, and never license to drop still-valid findings. Inline discussions are never updated or deleted — the dedup set prevents reposts.

If `summary` was null, create a fresh discussion (`project-merge-request-discussion create --mr-iid <iid> --body "..."` with no `--position`).

If there are zero discussion-only findings AND zero inline findings AND no prior summary, write **nothing** — don't post an empty summary.

A question embedded in an older summary's *Questions* section has no `(inline, question, file, anchor)` fingerprint — only `kind=summary` does. On re-review, post it inline (new fingerprint) AND rewrite the summary to drop the prose copy, both in one run — convergence is single-pass.

## Step 7 — Return status to the harness

Final assistant message in delivery mode: one short line, shaped like below — **distinct** from Step 6's similar-looking index line (this is the harness message; that lives in the summary discussion).

```
Posted 3 inline + updated summary on MR !128 — 5/5 detectors · 11 candidates → 3 inline, 1 demoted to summary, 1 duplicate skipped (rest refuted).
```

The `N/M detectors` field is **dispatched / expected** from the Stage 1 reconciliation — if a detector failed to load, name it (e.g. `4/5 detectors (cr-security unavailable)`). The candidate count is `merge.candidates`; account for the gap between it and what shipped (demoted to summary, duplicates skipped, refuted). Fold in every entry from `merge.notes` — failed detectors, malformed drops, collapsed duplicates. When the merge was all-zero, report the detector count and a `0 findings` tail. When the Stage 1 triage gate reviewed inline (no fan-out), write `inline (triage)` in place of the `N/M detectors` field — `candidates` is then the inline pass's pre-refutation count, so the accounting reads the same.

Do **not** return the review markdown when delivery succeeded — the comments are the deliverable.

## Delivery-phase error recovery

- If posting an inline finding fails (HTTP error, invalid position, etc.), demote it to discussion-only and continue. If the summary post fails too, return the full review as markdown and surface the error.
- If the `gitlab` tool isn't loadable or returns 403s, demote to interactive mode and return markdown.
- Never re-invoke the `skill` tool to restart the review.

## Reference material (optional)

- `references/marker-format.md` — per-field meaning, daiv-authored detection, resolution semantics.
- `examples/example-review-output.md` — a well-formed inline + summary delivery example.
