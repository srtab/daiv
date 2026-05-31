# GitLab Delivery

Delivery mode only. You reach here after `references/review-workflow.md` has produced verified findings. Deliver them as inline discussions on specific lines plus one top-level summary discussion, posting directly via the `gitlab` tool. The review is done — you are now publishing it, not re-adjudicating it.

Relative paths (`scripts/…`, `references/…`, `examples/…`) resolve under the skill root injected in `SKILL.md`.

## Inputs (from the review workflow)

Carry these in from the verified-findings handoff:

- **scope** + the SHA triplet (`base_sha`, `start_sha`, `head_sha`). Markers and positions use `head_sha`.
- the **verified findings**, each with `detector`, `file`, `line`, `bar`, `archetype`, `title`, `rationale`, optional `suggestion`, and assigned **severity**.
- **detector status** (dispatched / expected) and **merge stats** (`candidates` / `dropped` / `merged`) for the Step 7 status line.

**Even when there are zero verified findings, do not skip delivery.** Steps 1, 2, and 6 still run — parse existing notes, address any pending replies on prior daiv threads, and reconcile the summary (update it in place; write nothing new if none exists). Only the new-finding work (Steps 3–5) is skipped.

## Marker format

`scripts/marker.py` is the canonical implementation of the marker contract — **never compute anchors or assemble markers by hand.** Its `anchor`, `build`, and `parse-notes` subcommands are deterministic and version-stable; paraphrasing the rules into ad-hoc Python or prose silently breaks dedup across reruns. Run `scripts/marker.py <cmd> --help` for arguments. The marker payload shape, per-field meanings, the daiv-authored detection rule (prefix, not author), and resolution semantics all live in `references/marker-format.md` — open it when a field's purpose is unclear.

This procedure decides only two things; the script does the rest:

- **Anchor target — which line.** Inline findings anchor on **added or context** lines on the new side of the diff. A pure-deletion finding (no `new_line`) is not inline-eligible — demote it to the summary. For a single-line finding (suggestion or question), the target is `new_line` from the diff position. For a multi-line finding (`suggestion:-N+M`, or a question over a contiguous block), the target is the **first** new-side line of the range. The model picks the line; the script computes the anchor.
- **Dedup fingerprint — what to compare.** Inline findings dedup on `(kind, archetype, file, anchor)`; the summary on `kind=summary` (exactly one summary daiv note per MR). Step 4 builds these.

## Step 1 — Acquire context and dedup state

- Read `merge_request_id`, project, and the SHA triplet from the runtime merge-request context. If any field is missing, demote to interactive mode and return markdown.
- Call the `gitlab` tool with `project-merge-request-discussion list --mr-iid <iid>` to list existing discussions on the MR. Then, in a separate `bash` call, feed that JSON to `scripts/marker.py parse-notes` on stdin (e.g. `echo '<json>' | python3 scripts/marker.py parse-notes`). The `gitlab` tool is not callable from `bash`, so you cannot compose the two with a shell pipe in a single call. `parse-notes` accepts no flags — its sole input is the discussion JSON on stdin. It returns:
  ```json
  {"inline_fingerprints": [["inline", "<archetype>", "<file>", "<anchor>"], ...],
   "summary": {"discussion_id": "...", "note_id": ...} | null,
   "pending_replies": [
     {"kind": "inline"|"summary",
      "discussion_id": "...",
      "notes": [{"author": "<username>", "body": "<text>", "system": false}, ...]},
     ...
   ]}
  ```
  Keep all three: `inline_fingerprints` is the **dedup set** for Step 4; `summary` tells Step 6 whether to update in place or create a fresh discussion; `pending_replies` lists unresolved daiv threads with at least one note after daiv's last — usually a human reply, but the trailing note can be a system event (flagged `system: true`), handled in Step 2. The script projects each note down to `author` / `body` / `system` — that is all the model needs to choose a Step 2 outcome.

## Step 2 — Address pending replies

For each discussion in `pending_replies`, read the conversation (the full `notes` array is included) and decide the outcome. Every thread here is already open — `parse-notes` excludes resolved threads upstream, so you never need to re-check `resolved`. Skip any thread where the only notes after daiv's last are `system: true` (e.g. a label change or resolve event) — there's nothing to respond to.

| Outcome | Action |
|---|---|
| **Daiv concedes** — false positive, user's reasoning is correct, or finding no longer applies | Post a brief acknowledgment reply, then **resolve the thread** (resolve command below). |
| **User concedes** — user accepts the finding, either by saying so or by already pushing a commit that addresses it | Post a brief acknowledgment reply. **Do not resolve** — let the user resolve when they apply the fix. |
| **Disagreement persists** — user pushes back, daiv still believes the finding holds | Post a defense reply that adds new evidence or restates the concern concretely. **Do not resolve.** |

Daiv resolves only when daiv withdraws. Posting prose without resolution is the default; resolution is the exception. Resolving a withdrawn finding does **not** leak a future repost — the marker stays on the resolved note, so the fingerprint stays in the next run's dedup set and the same finding is skipped.

Build each reply's marker with `scripts/marker.py build --kind reply --sha <head_sha>` and place its output as the first physical line of the reply body.

Post the reply:

```
gitlab project-merge-request-discussion-note create --mr-iid <iid> --discussion-id <discussion_id> --body "<reply body>"
```

Resolve the thread (only in the *Daiv concedes* case):

```
gitlab project-merge-request-discussion update --mr-iid <iid> --id <discussion_id> --resolved true
```

Note the `--id` flag means different things across subcommands: on `discussion update` it is the **discussion** id; on `discussion-note update` it is the **note** id. The reply create subcommand takes `--discussion-id` for the parent thread.

Keep replies short. If there is nothing concrete to add beyond "noted", post nothing and leave the thread for the user.

After addressing all pending replies, continue to bucketing new findings. A reply does not become a dedup entry — the original finding's fingerprint already prevents reposts.

## Step 3 — Bucket findings

Inline delivery comes in two shapes — both anchor on a single new-side line (or contiguous new-side range) and both dedup by `(inline, archetype, file, anchor)`.

**Fix archetypes** — the finding ships a concrete code change expressible as a `suggestion` block replacing a contiguous range of new-side lines within a single hunk:

- `remove_dead_lines`
- `use_framework_idiom`
- `replace_with_constant`
- `swap_library_call`

The range may cover several lines (use `suggestion:-N+M`) — what matters is that it's one contiguous hunk replacement, not that only one line changes. Lines inside the range that don't change are restated verbatim in the suggestion. For example, a finding that the same expression is computed twice across a 4-line block is still inline: the suggestion replaces all 4 lines, restating the ones that stay.

**Question archetype** — `question`. A targeted question that anchors on a single new-side line or a contiguous new-side range within a single hunk, and poses a concrete hypothesis the author can answer yes/no. No `suggestion` block — just marker + one or two sentences ending in `?`. Use this for every finding that meets the Signal filter's *Question* bar; the reader sees the question on the exact line(s) in the diff view instead of hunting for it in the summary. When anchoring on a range, follow the same rule as multi-line suggestions: post the discussion against the full range, and compute the anchor on the **first** new-side line of the range. Pure-deletion questions (no new-side line) demote to summary, same rule as fix archetypes.

Demote to **discussion-only** only when no clean inline shape exists: the finding spans multiple files or non-adjacent hunks, requires prose to land (e.g., "this module needs restructuring"), is a question with no single-line anchor (e.g., a cross-cutting concern about a refactor), is a question whose subject is a rename (the question is about call-site impact the anchor can't show), or is a rename that propagates to call sites.

A rename is *not* inline-eligible — a `suggestion` block patches only the declaration, not the call sites (worked example: `references/few-shot-examples.md` Part 2). Renames go in the summary.

If an inline finding's diff position cannot be constructed reliably (file renamed across the diff, line moved within a hunk, anchor ambiguous), **demote it to discussion-only**. Never post a misaligned suggestion or a misanchored question.

## Step 4 — Apply dedup

For each candidate, compute the anchor with `scripts/marker.py anchor --target "<target line>" --next "<next non-blank new-side line>"` and form the fingerprint `["inline", archetype, file, anchor]`. Always pass `--next` (the next non-blank new-side line) on every call — the script uses it only when it needs to disambiguate a short or all-separator target (`references/marker-format.md` has the exact rule), so there's no judgment call about when to pass it. **Skip if the fingerprint matches the dedup set** — do not rephrase, do not "post a stronger version." Only surface fingerprints not already present.

The fingerprint is anchored on line *content*, so two distinct findings on byte-identical target lines in different hunks of the same file (same archetype) collide on one fingerprint (`references/marker-format.md` documents why). Two cases, opposite handling:

- **Collision against the dedup set (a prior run posted it):** skip, as above — that's the dedup working.
- **Collision against a candidate you've already posted *this run*:** do **not** silently skip the second — it is a different, valid finding. Demote it to discussion-only (Step 6) so it still lands in the summary. Silently dropping it would lose a real finding.

## Step 5 — Post inline findings

For each surviving inline finding:

1. Build the marker line with `scripts/marker.py build --kind inline --sha <head_sha> --archetype <X> --file <new_path> --line <new_line> --anchor <anchor>`. Capture the output verbatim — it is the first physical line of the note body.
2. Post via `gitlab project-merge-request-discussion create` with `--position`. Follow the position-construction and suggestion-block guidance already documented on the `gitlab` tool — do not invent your own conventions.

Body shape depends on the archetype:

- **Fix archetype** (`remove_dead_lines`, `use_framework_idiom`, `replace_with_constant`, `swap_library_call`): marker line, then a short comment (one or two sentences), then a `suggestion` block replacing the target range. The suggestion block IS the value.
- **Question archetype** (`question`): marker line, then one or two sentences ending in `?`. No `suggestion` block. The question itself IS the value.

Keep bodies tight; the prose around it is justification, not filler. Example marker lines:

```
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"remove_dead_lines","file":"services/api.py","line":42,"anchor":"a1b2c3d4","sha":"abc1234"} -->
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"question","file":"env_files/all/grafana.env","line":9,"anchor":"b2c3d4e5","sha":"abc1234"} -->
```

## Step 6 — Post or update the summary discussion

Compose **one** top-level summary containing:

- All discussion-only findings, grouped by severity (High / Medium / Low — use the severity assigned in the review workflow). Same shape as the interactive Findings list: one-line summary + `<details>` block.
- A short **Questions** section for discussion-only questions (cross-file or un-anchored). Targeted questions go inline (see Step 3), not here.
- A one-line index of the inline findings posted this run (filename + line + archetype), so a reviewer skimming the thread sees the full picture without expanding diffs.

**Delta-only re-reviews must not shrink the summary.** When this run only re-examined a delta (`review-workflow.md` scope rule), its discussion-only findings cover only the changed area. A discussion-only finding has **no** inline fingerprint — only the single `kind=summary` marker exists — so `parse-notes` cannot recover it; the prior summary body is the only record, and you already have it in the Step 1 `discussion list` output (the JSON you fed to `parse-notes`). Re-read that body and **carry forward every discussion-only finding whose subject lies outside this run's delta**: it was not rechecked, so it is neither confirmed nor disproved, and rewriting the note without it would silently erase a still-valid finding. Drop a prior finding only when this run actively disproves it, or reposts it inline (the inline copy supersedes the summary prose). The body you post is the **union** of carried-forward prior findings and this run's new findings — never this run's findings alone. On a *full* re-review (every hunk re-detected) there is nothing outside the delta, so the union reduces to this run's findings and no special handling is needed.

Build the marker with `scripts/marker.py build --kind summary --sha <head_sha>` and place its output as the first physical line of the body. Example:

```
<!-- daiv-cr {"v":1,"kind":"summary","sha":"abc1234"} -->
```

If `summary` from Step 1 was non-null, **update the existing note in place** (`gitlab project-merge-request-discussion-note update --mr-iid <iid> --discussion-id <discussion_id> --id <note_id> --body "..."`). Recall the `--id` overloading from Step 2: on `discussion-note update` it is the **note** id — pass `summary.note_id` from Step 1 here, not the discussion id. The summary always reflects the current review state — every finding that still holds (this run's, plus the carried-forward prior findings from the rule above), not an append-only log of past reviews. "Update in place, not appended" governs the *note*: one summary discussion per MR, never a second; it does not license dropping still-valid findings. Inline discussions are never updated or deleted — the dedup set prevents reposts.

If `summary` was null, create a fresh top-level discussion (`project-merge-request-discussion create --mr-iid <iid> --body "..."` with no `--position`).

If there are zero discussion-only findings AND zero inline findings AND no prior summary, write **nothing** — don't post an empty summary.

A question previously embedded in an older summary's *Questions* section has no `(inline, question, file, anchor)` fingerprint — only the summary's `kind=summary` fingerprint exists. So on re-review the agent posts the question inline (new fingerprint, not deduped) AND rewrites the summary in place to drop the prose copy. Both happen in one run; convergence is single-pass.

## Step 7 — Return status to the harness

Final assistant message in delivery mode: one short line. Use the shape below. The summary-body footer (the index line in Step 6) may carry the same shape, but the two are **distinct strings** — this one is the harness message; that one lives inside the summary discussion.

```
Posted 3 inline + updated summary on MR !128 — 5/5 detectors · 11 candidates → 3 inline, 1 demoted to summary, 1 duplicate skipped (rest refuted).
```

The `N/M detectors` field is **dispatched / expected** from the Stage 1 reconciliation, not a hardcoded `5/5`: if a `cr-*` detector failed to load (absent from the `task` tool's agent list), report e.g. `4/5 detectors (cr-security unavailable)` so the missing dimension is visible. The candidate count is `merge.candidates` (the pre-refutation count from Stage 2, after cross-detector dedup); account for the gap between it and what shipped — demoted to summary, duplicates skipped, refuted — so the line reads cleanly. When `merge.dropped` is nonzero, note it too (e.g. `2 malformed dropped`) — a detector emitting schema-invalid findings is a real signal worth surfacing, not hiding. When the Stage 2 merge was short-circuited (every detector empty), `candidates`/`dropped`/`merged` are all `0`; report the detector count and a `0 findings` tail.

Do **not** return the review markdown when delivery succeeded — the comments are the deliverable.

## Delivery-phase error recovery

- If posting a specific inline finding fails (HTTP error, invalid position, etc.), demote that finding to discussion-only and continue with the rest. If the summary post also fails, return the full review as markdown so the harness can deliver it; surface the posting error in your final message.
- If the `gitlab` tool isn't loadable or returns 403s for the discussion endpoint, demote to interactive mode and return markdown.
- Never re-invoke the `skill` tool to restart the review.

## Reference material (optional)

- `references/marker-format.md` — per-field meaning of the `<!-- daiv-cr … -->` marker, daiv-authored detection, and resolution semantics. Open when a marker field's purpose is unclear.
- `examples/example-review-output.md` — a complete, well-formed example of inline + summary delivery output. The shape to match in delivery mode.
