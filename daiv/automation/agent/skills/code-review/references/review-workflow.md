# Review Workflow

The review itself: establish scope, detect, and verify. This produces the **verified findings** both modes consume. `SKILL.md` routes you here; follow it top to bottom. Delivery mechanics (posting to GitLab) live in `references/gitlab-delivery.md` — not here.

Relative paths (`scripts/…`, `references/…`, `examples/…`) resolve under the skill root injected in `SKILL.md`.

## Establish scope and inputs

- If you already reviewed this branch earlier in this conversation, do not start from scratch. Identify what changed since (new commits, force-pushed changes), and focus only on the delta. Do not re-fetch MR metadata or re-explore unchanged files.
- If a merge/pull request is referenced:
  1. fetch the MR/PR to determine source/target branches and the SHA triplet (`base_sha`, `start_sha`, `head_sha`) needed for inline anchors;
  2. fetch the diffs using `git diff <target>...<source>`. If `bash` fails, fall back to the platform tool.
- If a diff is already provided, review it directly.
- If scope is ambiguous, infer from conversation history and available artifacts. Otherwise, ask the user.

## Stage 0 — Check for per-repo review rules

Before detecting, check which rule sources the repo actually has on disk: `.agents/review-rules.md` (authoritative) and `AGENTS.md` / `.agents/AGENTS.md` (supplementary). If **any** of them exists, the `cr-custom-rules` detector runs in Stage 1 against the ones present; if **none** exists, **skip the `cr-custom-rules` detector**.

## Stage 1 — Detect (fan-out)

Dispatch the detectors **in parallel** with the `task` tool — one `task` call per detector, all issued in a single turn. Set each call's **`subagent_type`** to the detector's name: `cr-correctness`, `cr-security`, `cr-performance`, `cr-structure`, and (conditionally) `cr-custom-rules`. These are pre-defined subagents whose charter is their own system prompt, so you do **not** restate the charter.

**Never dispatch the detection to `general-purpose`** (or any other agent) with the dimension described in the prompt. The `cr-*` subagents carry their charter *and* a structured `response_format` — a `general-purpose` dispatch returns free-form prose with no `findings` array, which breaks the Stage 2 merge. If a `cr-*` type is not in the `task` tool's agent list it failed to load; skip it and report the gap (below) — do **not** substitute `general-purpose`. Each detector returns a structured object `{"findings": [...]}` conforming to the finding schema below — and nothing else.

**Reconcile against what's actually available.** The detectors that loaded successfully are the `cr-*` subagent types the `task` tool lists. Before dispatching, work out your *expected* set — the four built-ins plus `cr-custom-rules` when Stage 0 found a rule source — and note any expected detector that the `task` tool does **not** offer: it failed to load, so its dimension won't be covered this run. Carry both numbers (dispatched / expected) into the delivery status line (`gitlab-delivery.md` Step 7) or the interactive output so a missing dimension is reported, not silently absent. If a `cr-*` type is unavailable, dispatch the rest; never abort the review over one missing detector.

- `cr-correctness` — logic/parse defects, breaking schema/contract changes, concurrency, error handling, side effects, absent-value, config/env.
- `cr-security` — input validation at trust boundaries, authz/authn, secrets exposure.
- `cr-performance` — N+1 / repeated calls or lookups in loops, obvious inefficiencies.
- `cr-structure` — dead lines, unused framework idioms, misplaced logic, missed reuse, misleading naming, magic values, typing, logging, i18n, a11y.
- `cr-custom-rules` — enforces the repo's review rules. **Dispatch only if a rule source exists** (Stage 0: `.agents/review-rules.md`, `AGENTS.md`, or `.agents/AGENTS.md`); pass it the paths of the ones present.

Two naming registers, don't conflate them: the **subagent type** you pass to `task` is `cr-<dimension>` (e.g. `cr-custom-rules`), while the `detector` field each finding carries is the bare `<dimension>` (e.g. `custom-rules`). Subagent `cr-correctness` → `detector: "correctness"`, and so on.

Pass into every detector's `task` prompt only the **scope**: the change under review as source/target refs + the SHA triplet and the changed-file list — **not the diff itself** (it can be long; the detector runs git commands to fetch the hunks it needs) — plus the new-side path scope, so all detectors review the same change. The `cr-custom-rules` detector additionally receives the **paths** of the rule sources that exist (not their contents — it opens them itself).

The detectors already carry their charter, the Signal-filter bars, and the never-flag rules (style, formatting, whitespace, import ordering) in their system prompts; your prompt supplies only the scope.

### Finding schema

Each detector returns a structured object `{"findings": [ ... ]}` whose items are objects in this exact shape (canonical machine copy: `scripts/finding.schema.json`):

```json
{"detector":"correctness|security|performance|structure|custom-rules","file":"<new_path>","line":42,"bar":"defect|structural|question","archetype":"remove_dead_lines|use_framework_idiom|replace_with_constant|swap_library_call|question|discussion","title":"<one line>","rationale":"<why it's a problem>","suggestion":"<optional, fix archetypes only>","source":"<custom-rules only: the rule enforced>"}
```

- **`bar`** — the Signal-filter class (`defect` / `structural` / `question`).
- **`archetype`** — one of the six schema values only: the four inline fix types (`remove_dead_lines`, `use_framework_idiom`, `replace_with_constant`, `swap_library_call`), `question`, or `discussion` for everything else (renames, cross-file or structural findings, anything that needs prose).
  - **Only those six strings are valid.** The named discussion patterns in `references/few-shot-examples.md` Part 2 (e.g. `rename`, `move_to_other_module`) are documentation labels, **not** schema values — they all serialize as `archetype: "discussion"`. Emitting a label like `archetype: "rename"` makes `findings.py` drop the finding as malformed, so it silently vanishes. The parent finalizes inline-eligibility during delivery bucketing (`gitlab-delivery.md` Step 3).
- **`source`** — required on `custom-rules` findings (enforced by `findings.py`) so the posted comment can cite the rule.

`scripts/findings.py merge` validates the required fields, the enums, and the `custom-rules` `source` rule — not the `suggestion`/archetype coupling, which delivery bucketing handles.

## Stage 2 — Verify (adjudicate)

Collect each detector's `findings` array (from its returned `{"findings": [...]}` object) and concatenate them into one JSON array. **If every detector returned an empty array, skip the merge entirely** — there is nothing to validate or dedup; treat `candidates`, `dropped`, and `merged` as `0`. In **interactive mode** this means "No findings." In **delivery mode** an empty result does *not* mean skip delivery: the reconciliation steps still run (`gitlab-delivery.md` covers exactly which). Run the merge only when at least one finding exists:

```
echo '<combined findings JSON array>' | python3 scripts/findings.py merge
```

`merge` validates each finding against the schema (dropping malformed ones) and collapses cross-detector duplicates on `(file, line, archetype)`, keeping the strongest `bar`. It returns `{"findings":[...], "candidates":N, "dropped":M, "merged":K}`; the `merge()` docstring in `scripts/findings.py` defines each field. Carry `candidates` (the pre-refutation count) into the status line, and note `merged` there when nonzero so a reviewer knows findings sharing a `(file, line, archetype)` key were collapsed. This is the pre-delivery cross-detector dedup — distinct from the anchor-based delivery dedup on `(kind, archetype, file, anchor)` in `gitlab-delivery.md` Step 4.

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

Never include self-corrected findings, strikethrough, or "on closer reading this is fine" in the output. Reason internally, present only confirmed survivors. Over-pruning is acceptable — precision first. The survivors are what gets delivered.

### Severity

Assign each verified finding a severity now; it travels with the finding as data. Both the interactive output and the delivery summary (`gitlab-delivery.md` Step 6) group by this assigned severity — they do **not** recompute it. The mapping follows from `bar` and detector deterministically:

- **High** — a `defect` from `correctness`, `security`, or `custom-rules` (wrong results, broken authz, data loss, a violated binding rule).
- **Medium** — a `defect` from `performance`, or a `structural` concern that spans files or changes a behavior/contract.
- **Low** — a local `structural` concern (dead lines, magic values, a single-spot idiom, misleading naming).
- `question` findings are **not** severity-graded — they go in the *Questions* section, never a High/Medium/Low bucket.

The Medium/Low rows grade `structural` concerns by **scope, not detector** — a `structural` finding from any detector, `custom-rules` included, lands in Medium (spans files / changes a contract) or Low (local). Only `defect` severity is detector-specific (the High/Medium split above).

## Verified-findings handoff

At this point the workflow has produced everything the rest of the run needs:

- **scope** + the SHA triplet (`base_sha`, `start_sha`, `head_sha`; `head_sha` is what markers and positions use);
- the **verified findings**, each carrying `detector`, `file`, `line`, `bar`, `archetype`, `title`, `rationale`, optional `suggestion`, and the assigned **severity**;
- **detector status** — dispatched / expected from the Stage 1 reconciliation;
- **merge stats** — `candidates` / `dropped` / `merged` from Stage 2 (all `0` when the merge was short-circuited).

This is the seam between reviewing and delivering. In delivery mode, stop adjudicating here and switch modes: open `references/gitlab-delivery.md` and deliver these survivors. In interactive mode, render them with the protocol below.

## Interactive mode output

Use the markdown format below. Return the review as the final assistant message; the harness posts it. **Do NOT post the review as a comment yourself in interactive mode.**

### Findings

Numbered list grouped by severity (High / Medium / Low, from the assigned severity above). Each finding has a one-line summary with the file reference, and a collapsible `<details>` block for the explanation and fix. When the finding is a fix archetype, include the concrete fix as a fenced code block inside the `<details>`.

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

## Workflow-phase error recovery

- If a tool call fails, switch to an alternative (e.g. platform tool instead of `bash git diff`) and continue. Never re-invoke the `skill` tool to restart the review.
- If the `task` tool can't fan out the detectors in parallel (rejected, or a detector `task` errors), fall back to dispatching them sequentially, or run the detection inline yourself — don't abort the review.

## Reference material (optional)

When a finding's framing is unclear, open the relevant section of:

- `references/principles.md` — generic, code-agnostic principles per category, derived from a corpus of human reviews. The *why* behind a finding's body.
- `references/few-shot-examples.md` — real comment→fix pairs per archetype, with before/after code. Use to calibrate how short a useful comment can be and what a suggestion block typically replaces.

Read only the section you need. These are not required reading on every review.
