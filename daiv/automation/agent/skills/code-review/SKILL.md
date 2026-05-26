---
name: code-review
version: 2.0.0
description: This skill should be used when a user asks for a code review, feedback on a PR or MR, diff assessment, or says things like 'can you review my changes', 'look at this diff', 'is this ready to merge', 'check my code', 'review this branch', 'what do you think of these changes', or 'LGTM check'. Covers correctness, tests, performance, security, structural concerns, and questions of intent on pull/merge requests or raw diffs from any platform (GitHub, GitLab).
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

## What to look for

Five patterns produce most of the value in human review — scan the diff for each:

1. **Dead lines** — overridden defaults equal to the default, unused parameters, commented-out blocks, leftover instrumentation, branches with no reachable consumer.
2. **Framework idioms not used** — the library/framework already provides this; the diff reimplements it. Inline custom code where a one-line built-in would replace it.
3. **Misplaced logic** — code added in a layer that doesn't own it (business rules in a template, validation done in a view that has a model layer, container config in an orchestration file that belongs in the image).
4. **Reuse missed** — the same shape appears elsewhere in the diff or surrounding code. Point at the existing target.
5. **Naming that misleads** — type/variable/function names that describe a smaller, larger, or different concept than the code actually does.

Beyond those, also check:

- Correctness defects (compile/parse errors, clearly wrong logic).
- Input validation gaps at trust boundaries.
- Security exposures (secrets in code, broken authz).
- Concurrency hazards (locking missing on contested writes, races).
- Unintended side effects (a hook now firing in paths it didn't before).

Do **not** flag style, formatting, whitespace, or import ordering — tooling handles those. Don't flag naming unless the name materially misleads.

## Signal filter

A finding must meet one of three bars to ship:

- **Defect** — the code will fail to compile/parse or produce wrong results on common inputs. You could write the failing test without knowing the runtime environment.
- **Structural concern** — points at a specific line and proposes, in the next sentence, a concrete change: `use X instead of Y`, `move to file Z`, `delete lines L-M`, `extract to helper at A`. If the recommendation is vague ("consider cleaning this up"), the finding isn't ready.
- **Question** — points at a specific line with a concrete hypothesis ("does this trigger an email on every save, not just on create?"). The answer needs the author's intent, not the diff. Don't ask curiosity questions, don't paraphrase the code as a question.

If you reconsider a finding during analysis and conclude it isn't a real issue, drop it entirely. Never include self-corrected findings, strikethrough text, or "on closer reading this is fine" in the output. Reason internally, present only confirmed findings.

## Delivery mode protocol

### Marker format

Every note daiv posts begins with a single-line HTML comment carrying a JSON payload:

```
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"...","file":"...","line":42,"anchor":"a1b2c3d4","sha":"abc1234"} -->
```

Fields:

- `v` — marker schema version (currently `1`). Markers with an unknown `v` are treated as **not in the dedup set** (never collide with current findings).
- `kind` — `inline` or `summary`.
- `archetype` — inline-eligible archetype name (inline only).
- `file` — `new_path` from the diff (inline only).
- `line` — `new_line` from the diff (inline only). **Diagnostic only — not used in dedup**, because line numbers shift on unrelated commits.
- `anchor` — stable identity for inline findings (see *Anchor algorithm* below).
- `sha` — `head_sha` at posting time (both kinds). Diagnostic only.

**Anchor algorithm.** `anchor` = first 8 hex chars of `sha256(anchor_input)`. The algorithm is **deterministic** — apply the same steps on every review; never branch based on whether the current batch of findings happens to collide. Branching at posting time would silently break dedup across reruns.

1. Inline findings anchor on **added or context** lines on the new side of the diff. A pure-deletion finding (no `new_line`) is not inline-eligible — demote it to the summary.
2. For a single-line suggestion, the target line is `new_line` from the diff position. For a multi-line suggestion (`suggestion:-N+M` covering several lines), the target line is the **first** new-side line of the replacement window.
3. Let `t = strip(content_of_target_line)`.
4. If `len(t) < 16` OR `t` matches `^[\s\}\)\]\;\,\.]+$` (closing braces, separators, short returns), append the next non-blank new-side line (stripped), separated by `\n`. This disambiguator is **unconditional** — apply it on every run for any line meeting the criterion, even when no collision exists in the current batch. If no next non-blank line exists (end of file), use `t` alone and accept the rare collision.
5. Otherwise `anchor_input = t`.

**Serialization.** The marker must be a single physical line — no embedded newlines. JSON string values follow RFC 8259 escaping; file paths containing `"` or `\` must be escaped as `\"` and `\\` respectively.

**Daiv-authored detection.** A note is treated as daiv-posted **iff** its body begins with the literal prefix `<!-- daiv-cr ` followed by a parseable JSON payload terminating in ` -->`. Author username is *not* used. If the JSON doesn't parse, or required fields are missing, ignore the note for dedup purposes.

**Dedup fingerprint:**

- Inline: `(kind, archetype, file, anchor)`.
- Summary: `kind=summary` — exactly one summary daiv note may exist per MR.

### Step 1 — Acquire context and dedup state

- Read `merge_request_id`, project, and the SHA triplet from the runtime merge-request context. If any field is missing, demote to interactive mode and return markdown.
- List existing discussions on the MR with `gitlab project-merge-request-discussion list --mr-iid <iid>`.
- For each note whose body begins with the daiv marker prefix, parse the JSON payload (see *Marker format* above) and apply the daiv-authored detection rule. Build the **dedup set** of inline fingerprints `{(kind, archetype, file, anchor)}` and remember the discussion_id and note_id of any existing `kind=summary` daiv note — you will update it in place, not repost.

### Step 2 — Bucket findings

For each candidate finding, classify the fix as one of these **inline-eligible** archetypes:

- `remove_dead_lines`
- `use_framework_idiom`
- `replace_with_constant`
- `swap_library_call`

If the fix is one of those AND fits in one or a few contiguous lines AND has a concrete suggested replacement, it's **inline**. Anything else (structural concerns spanning multiple lines or files, questions, renames that propagate to call sites, anything that needs prose to land) is **discussion-only**.

A rename is *not* inline-eligible: a `suggestion` block can only patch the declaration, not the call sites, so a rename-as-inline ships a half-truth. Renames go in the summary.

If an inline finding's diff position cannot be constructed reliably (file renamed across the diff, line moved within a hunk, anchor ambiguous), **demote it to discussion-only**. Never post a misaligned suggestion.

### Step 3 — Apply dedup

For each candidate, compute its inline fingerprint `(kind, archetype, file, anchor)` as defined in *Marker format*. **Skip if it matches the dedup set** — do not rephrase, do not "post a stronger version." Only surface fingerprints not already present.

### Step 4 — Post inline findings

For each surviving inline finding, post via `gitlab project-merge-request-discussion create` with `--position`. Follow the position-construction and suggestion-block guidance already documented on the `gitlab` tool — do not invent your own conventions.

The body **must start** with the marker header on the very first line, in the JSON format defined in *Marker format*. For example:

```
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"remove_dead_lines","file":"services/api.py","line":42,"anchor":"a1b2c3d4","sha":"abc1234"} -->
```

Then a short comment (one or two sentences), then — when the fix is a literal line replacement — a `suggestion` block. Keep bodies tight; the suggestion block IS the value, not the prose around it.

### Step 5 — Post or update the summary discussion

Compose **one** top-level summary containing:

- All discussion-only findings, grouped by severity (High / Medium / Low). Same shape as Interactive mode below: one-line summary + `<details>` block.
- A short **Questions** section, if any.
- A one-line index of the inline findings posted this run (filename + line + archetype), so a reviewer skimming the thread sees the full picture without expanding diffs.

Start the body with the summary marker, again in JSON form:

```
<!-- daiv-cr {"v":1,"kind":"summary","sha":"abc1234"} -->
```

If a `kind=summary` daiv note already exists (from a prior review), **update it in place** (`gitlab project-merge-request-discussion-note update --mr-iid <iid> --discussion-id <did> --id <note_id> --body "..."`). The summary always reflects the current review state, not history. Inline discussions are never updated or deleted — the dedup set prevents reposts.

If no summary existed, create a fresh top-level discussion (`project-merge-request-discussion create --mr-iid <iid> --body "..."` with no `--position`).

If there are zero discussion-only findings AND zero inline findings AND no prior summary, write **nothing** — don't post an empty summary.

### Step 6 — Return status to the harness

Final assistant message in delivery mode: one short line, e.g.

```
Posted 3 inline + updated summary on MR !128 (skipped 1 duplicate, demoted 1 to summary).
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

- `references/principles.md` — generic, code-agnostic principles per category, derived from a corpus of human reviews. The *why* behind a finding's body.
- `references/few-shot-examples.md` — real comment→fix pairs per archetype, with before/after code. Use to calibrate how short a useful comment can be and what a suggestion block typically replaces.

Read only the section you need. These are not required reading on every review.
