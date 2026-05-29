---
name: code-review
description: This skill should be used when a user asks for a code review, feedback on a PR or MR, diff assessment, or says things like 'can you review my changes', 'look at this diff', 'is this ready to merge', 'check my code', 'review this branch', 'what do you think of these changes', or 'LGTM check'. Covers correctness, tests, performance, security, structural concerns, and questions of intent on pull/merge requests or raw diffs from any platform (GitHub, GitLab).
metadata:
  version: 3.0.0
---

# Code Review

## Mode of operation

Pick one before reviewing:

- **Delivery mode**: the runtime has merge-request context (`Scope.MERGE_REQUEST` with a `merge_request_id`) AND the platform is GitLab. The review is delivered as inline discussions on specific lines plus one top-level summary discussion. The skill posts directly via the `gitlab` tool.
- **Interactive mode**: anything else — a local diff, a referenced MR/PR with no runtime context, a GitHub PR, or scope is ambiguous. The review is returned as a markdown final message; the harness handles delivery.

Delivery mode requires the `gitlab` tool. If it's not loaded, `tool_search` for it before continuing. If it can't be loaded (or returns 403 on the discussion endpoint), demote the whole review to interactive mode.

## Establish scope and inputs

- If you already reviewed this branch earlier in this conversation, do not start from scratch. Identify what changed since (new commits, force-pushed changes), and focus only on the delta. Do not re-fetch MR metadata or re-explore unchanged files.
- If a merge/pull request is referenced:
  1. fetch the MR/PR to determine source/target branches and the SHA triplet (`base_sha`, `start_sha`, `head_sha`) needed for inline anchors;
  2. fetch the diffs using `git diff <target>...<source>`. If `bash` fails, fall back to the platform tool.
- If a diff is already provided, review it directly.
- If scope is ambiguous, infer from conversation history and available artifacts. Otherwise, ask the user.

### Read per-repo review rules (Stage 0)

Before detecting, resolve the repo's custom review rules so Stage 1 knows whether to run the `custom-rules` detector:

- Run `python3 scripts/rules.py resolve --root . --context-file <repo context file name, default AGENTS.md>`.
- If `has_rules` is `true`, keep the returned `rules_document` for the `custom-rules` detector.
- If `has_rules` is `false`, **skip the `custom-rules` detector** — the repo hasn't opted in.

`scripts/rules.py` reads ordinary repo files only and never changes config. It treats `.agents/review-rules.md` as authoritative and `AGENTS.md` / `.agents/AGENTS.md` as supplementary. Run `scripts/rules.py resolve --help` for details.

## Stage 1 — Detect (fan-out)

Dispatch the detectors **in parallel** with the `task` tool — one `task` call per detector, all issued in a single turn, `subagent_type=general-purpose`. Each detector owns a focused slice of the review and returns a JSON array of findings (schema below) **and nothing else**. The full charters — what each detector owns and the `principles.md` sections behind them — are in `references/detectors.md`. In summary:

- `correctness` — logic/parse defects, breaking schema/contract changes, concurrency, error handling, side effects, absent-value, config/env.
- `security` — input validation at trust boundaries, authz/authn, secrets exposure.
- `performance` — N+1 / repeated calls or lookups in loops, obvious inefficiencies.
- `structure` — dead lines, unused framework idioms, misplaced logic, missed reuse, misleading naming, magic values, typing, logging, i18n, a11y.
- `custom-rules` — evaluates the Stage 0 `rules_document`. **Dispatch only if `has_rules` was true**, and pass `rules_document` into the task prompt.

Pass into every detector's `task` prompt the change under review — the diff itself, or the source/target refs + SHA triplet and the changed-file list — plus the new-side path scope, so all detectors review the same change. (The `custom-rules` detector additionally receives `rules_document` from Stage 0.)

Tell each detector to read surrounding code for context before deciding, to apply the Signal-filter bars (Stage 2) when forming a finding, and to never flag style, formatting, whitespace, or import ordering.

### Finding schema

Every detector returns a JSON array of objects in this exact shape:

```json
{"detector":"correctness|security|performance|structure|custom-rules","file":"<new_path>","line":42,"bar":"defect|structural|question","archetype":"remove_dead_lines|use_framework_idiom|replace_with_constant|swap_library_call|question|discussion","title":"<one line>","rationale":"<why it's a problem>","suggestion":"<optional, fix archetypes only>","source":"<custom-rules only: the rule enforced>"}
```

`bar` is the Signal-filter class. Choose `archetype` from the six schema values only: the four inline fix types (`remove_dead_lines`, `use_framework_idiom`, `replace_with_constant`, `swap_library_call`), `question`, or `discussion` for everything else — renames, cross-file or structural findings, and anything that needs prose. The named discussion patterns in `references/few-shot-examples.md` Part 2 (e.g. `rename`, `move_to_other_module`) are documentation labels, not schema values — they all serialize as `archetype: "discussion"`; the parent finalizes inline-eligibility during Stage 3 bucketing. `source` is required on `custom-rules` findings (enforced by `findings.py`) so the posted comment can cite the rule. `scripts/findings.py merge` validates against this schema and is its canonical definition.

## Stage 2 — Verify (adjudicate)

Collect the JSON arrays the detectors returned, concatenate them into one JSON array, and run the deterministic merge:

```
echo '<combined findings JSON array>' | python3 scripts/findings.py merge
```

`merge` validates each finding against the schema (dropping malformed ones) and collapses cross-detector duplicates on `(file, line, archetype)`, keeping the strongest `bar`. It returns `{"findings":[...], "candidates":N, "dropped":M}`, where `candidates` is the count of distinct findings after cross-detector dedup (before adversarial verification) — keep it for the Step 7 status line. Note this `(file, line, archetype)` dedup is the pre-delivery cross-detector merge; delivery dedup against already-posted notes (Stage 3) is anchor-based `(file, anchor, archetype)` — two different stages, two different keys.

Then **adversarially verify** each surviving finding. For each one, build the strongest case that it is a false positive and **discard it unless it clearly survives**. Drop it if any of these hold:

- it's a pre-existing issue not introduced by this diff;
- it looks like a bug but isn't (you misread the control flow or context);
- it's a pedantic nitpick or pure style;
- it's a linter's or formatter's job;
- there's a lint-ignore or an intentional marker nearby;
- the code path isn't actually reachable or triggered.

A finding that survives refutation must also meet one of three bars (this is the Signal filter):

- **Defect** — the code will fail to compile/parse or produce wrong results on common inputs. You could write the failing test without knowing the runtime environment.
- **Structural concern** — points at a specific line and proposes, in the next sentence, a concrete change: `use X instead of Y`, `move to file Z`, `delete lines L-M`, `extract to helper at A`. Vague ("consider cleaning this up") doesn't ship.
- **Question** — points at a specific line with a concrete hypothesis ("does this trigger an email on every save, not just on create?"). The answer needs the author's intent, not the diff. No curiosity questions, no paraphrasing the code.

Never include self-corrected findings, strikethrough, or "on closer reading this is fine" in the output. Reason internally, present only confirmed survivors. Over-pruning is acceptable — precision first. The survivors are what Stage 3 delivers.

## Stage 3 — Deliver (delivery mode)

Deliver the Stage 2 survivors. The marker/anchor machinery below is unchanged — it owns dedup against already-posted notes, the summary, and reply handling.

### Marker format

Every note daiv posts begins with a single-line HTML comment carrying a JSON payload:

```
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"...","file":"...","line":42,"anchor":"a1b2c3d4","sha":"abc1234"} -->
```

Fields:

- `v` — marker schema version (currently `1`). Markers with an unknown `v` are ignored entirely — `parse-notes` drops them, so they don't dedup against current findings and they don't surface as `pending_replies` either.
- `kind` — `inline`, `summary`, or `reply`. Reply markers carry only `v`, `kind`, and `sha` — no archetype/file/line/anchor, since replies inherit their thread from the discussion they're posted to. They exist so daiv-authored detection stays uniform (one prefix rule for findings and replies, no author-username lookup).
- `archetype` — inline-eligible archetype name (inline only).
- `file` — `new_path` from the diff (inline only).
- `line` — `new_line` from the diff (inline only). **Diagnostic only — not used in dedup**, because line numbers shift on unrelated commits.
- `anchor` — stable 8-hex identity for inline findings, computed as the first 8 hex chars of `sha256` over the stripped target line (with a short-line/separator disambiguator that appends the next non-blank new-side line). Only the line content feeds the anchor — diagnostic fields don't.
- `sha` — `head_sha` at posting time (all kinds). Diagnostic only.

**Implementation.** `scripts/marker.py` is the canonical implementation of the marker contract — never compute anchors or assemble markers by hand. The script's `anchor`, `build`, and `parse-notes` subcommands are deterministic and version-stable; paraphrasing the rules into ad-hoc Python or prose silently breaks dedup across reruns. Run `scripts/marker.py <cmd> --help` for argument details.

**Anchor target.** Inline findings anchor on **added or context** lines on the new side of the diff. A pure-deletion finding (no `new_line`) is not inline-eligible — demote it to the summary. For a single-line finding (suggestion or question), the target line is `new_line` from the diff position. For a multi-line finding — `suggestion:-N+M` covering several lines, or a question scoped to a contiguous block — the target is the **first** new-side line of the range. The model picks the line; the script computes the anchor.

**Daiv-authored detection.** A note is treated as daiv-posted **iff** its body begins with the literal prefix `<!-- daiv-cr ` followed by a parseable JSON payload terminating in ` -->`. Author username is *not* used. `parse-notes` applies this rule; do not reimplement it.

**Dedup fingerprint:**

- Inline: `(kind, archetype, file, anchor)`.
- Summary: `kind=summary` — exactly one summary daiv note may exist per MR.

**Resolution semantics.** A discussion's `resolved` state does not affect dedup. If the user resolves a thread without applying the suggestion (or with any other outcome), the marker stays on the resolved note, so `parse-notes` still surfaces its fingerprint on the next review and the same finding is skipped. Resolution is a UX signal between humans, not an instruction to forget. The one thing `resolved` *does* affect is reply handling (Step 2): resolved threads are dropped from `pending_replies` since the conversation is closed.

### Step 1 — Acquire context and dedup state

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
  Keep all three: `inline_fingerprints` is the **dedup set** for Step 4; `summary` tells Step 6 whether to update in place or create a fresh discussion; `pending_replies` lists unresolved daiv threads whose last note is from a human (handled in Step 2). The script projects each note down to `author` / `body` / `system` — that is all the model needs to choose a Step 2 outcome.

### Step 2 — Address pending replies

For each discussion in `pending_replies`, read the conversation (the full `notes` array is included) and decide the outcome:

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

### Step 3 — Bucket findings

Inline delivery comes in two shapes — both anchor on a single new-side line (or contiguous new-side range) and both dedup by `(inline, archetype, file, anchor)`.

**Fix archetypes** — the finding ships a concrete code change expressible as a `suggestion` block replacing a contiguous range of new-side lines within a single hunk:

- `remove_dead_lines`
- `use_framework_idiom`
- `replace_with_constant`
- `swap_library_call`

The range may cover several lines (use `suggestion:-N+M`) — what matters is that it's one contiguous hunk replacement, not that only one line changes. Lines inside the range that don't change are restated verbatim in the suggestion. For example, a finding that the same expression is computed twice across a 4-line block is still inline: the suggestion replaces all 4 lines, restating the ones that stay.

**Question archetype** — `question`. A targeted question that anchors on a single new-side line or a contiguous new-side range within a single hunk, and poses a concrete hypothesis the author can answer yes/no. No `suggestion` block — just marker + one or two sentences ending in `?`. Use this for every finding that meets the Signal filter's *Question* bar; the reader sees the question on the exact line(s) in the diff view instead of hunting for it in the summary. When anchoring on a range, follow the same rule as multi-line suggestions: post the discussion against the full range, and compute the anchor on the **first** new-side line of the range. Pure-deletion questions (no new-side line) demote to summary, same rule as fix archetypes.

Demote to **discussion-only** only when no clean inline shape exists: the finding spans multiple files or non-adjacent hunks, requires prose to land (e.g., "this module needs restructuring"), is a question with no single-line anchor (e.g., a cross-cutting concern about a refactor), is a question whose subject is a rename (the question is about call-site impact the anchor can't show), or is a rename that propagates to call sites.

A rename is *not* inline-eligible: a `suggestion` block can only patch the declaration, not the call sites, so a rename-as-inline ships a half-truth. Renames go in the summary.

If an inline finding's diff position cannot be constructed reliably (file renamed across the diff, line moved within a hunk, anchor ambiguous), **demote it to discussion-only**. Never post a misaligned suggestion or a misanchored question.

### Step 4 — Apply dedup

For each candidate, compute the anchor with `scripts/marker.py anchor --target "<target line>" [--next "<next non-blank new-side line>"]` and form the fingerprint `["inline", archetype, file, anchor]`. Always pass `--next` when the target line might be short or all-separators — the script decides whether to use it. **Skip if the fingerprint matches the dedup set** — do not rephrase, do not "post a stronger version." Only surface fingerprints not already present.

### Step 5 — Post inline findings

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

### Step 6 — Post or update the summary discussion

Compose **one** top-level summary containing:

- All discussion-only findings, grouped by severity (High / Medium / Low). Same shape as Interactive mode below: one-line summary + `<details>` block.
- A short **Questions** section for discussion-only questions (cross-file or un-anchored). Targeted questions go inline (see Step 3), not here.
- A one-line index of the inline findings posted this run (filename + line + archetype), so a reviewer skimming the thread sees the full picture without expanding diffs.

Build the marker with `scripts/marker.py build --kind summary --sha <head_sha>` and place its output as the first physical line of the body. Example:

```
<!-- daiv-cr {"v":1,"kind":"summary","sha":"abc1234"} -->
```

If `summary` from Step 1 was non-null, **update the existing note in place** (`gitlab project-merge-request-discussion-note update --mr-iid <iid> --discussion-id <discussion_id> --id <note_id> --body "..."`). The summary always reflects the current review state, not history. Inline discussions are never updated or deleted — the dedup set prevents reposts.

If `summary` was null, create a fresh top-level discussion (`project-merge-request-discussion create --mr-iid <iid> --body "..."` with no `--position`).

If there are zero discussion-only findings AND zero inline findings AND no prior summary, write **nothing** — don't post an empty summary.

A question previously embedded in an older summary's *Questions* section has no `(inline, question, file, anchor)` fingerprint — only the summary's `kind=summary` fingerprint exists. So on re-review the agent posts the question inline (new fingerprint, not deduped) AND rewrites the summary in place to drop the prose copy. Both happen in one run; convergence is single-pass.

### Step 7 — Return status to the harness

Final assistant message in delivery mode: one short line, e.g.

```
Posted 3 inline + updated summary on MR !128 — 5/5 detectors, 3 of 11 candidates posted (skipped 1 duplicate, demoted 1 to summary).
```

Do **not** return the review markdown when delivery succeeded — the comments are the deliverable.

## Interactive mode protocol

Use the markdown format below. Return the review as the final assistant message; the harness posts it. **Do NOT post the review as a comment yourself in interactive mode.**

### Findings

Numbered list grouped by severity (High / Medium / Low). Each finding has a one-line summary with the file reference, and a collapsible `<details>` block for the explanation and fix.

```
**1. Summary of the issue** — [path/to/file.py:42](link)

<details>
<summary>Details</summary>

Explanation and fix.

</details>
```

Use the link format from the "Code References" section in the system prompt for all file locations. Place the file reference in the finding summary line, not in the body.

If there are no findings, write "No findings." and skip the section.

### Questions

Same shape as Findings. Each question must anchor on a specific file:line and pose a concrete hypothesis the author can answer yes/no. Omit if none.

## Error recovery

- If a tool call fails, switch to an alternative (e.g. platform tool instead of `bash git diff`) and continue. Never re-invoke the `skill` tool to restart the review.
- In delivery mode, if posting a specific inline finding fails (HTTP error, invalid position, etc.), demote that finding to discussion-only and continue with the rest. If the summary post also fails, return the full review as markdown so the harness can deliver it; surface the posting error in your final message.
- If the `gitlab` tool isn't loadable or returns 403s for the discussion endpoint, demote to interactive mode and return markdown.

For a complete example of well-formed inline and summary output, see `examples/example-review-output.md`.

## Reference material (optional)

When a finding's framing is unclear, open the relevant section of:

- `references/detectors.md` — the five detector charters: what each detector owns and the JSON it returns. Backs Stage 1 dispatch.
- `references/principles.md` — generic, code-agnostic principles per category, derived from a corpus of human reviews. The *why* behind a finding's body.
- `references/few-shot-examples.md` — real comment→fix pairs per archetype, with before/after code. Use to calibrate how short a useful comment can be and what a suggestion block typically replaces.
- `examples/example-review-output.md` — a complete, well-formed example of inline + summary delivery output. The shape to match in delivery mode.

Read only the section you need. These are not required reading on every review.
