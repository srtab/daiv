# Code-review skill restructure — align with pr-review-toolkit

**Date:** 2026-07-27
**Status:** Approved design, pending implementation plan

## Context

The code-review skill (v3.5.0) is a heavily engineered pipeline: `SKILL.md` routes into
`references/review-workflow.md`, which drives a staged flow — detector fan-out with forced
structured output, deferred JSON output files, `findings.py merge` with dedup accounting,
parent-side adversarial verification, severity mapping tables, and a GitLab delivery phase
built on `marker.py` anchors and SHA-triplet inline positions.

This machinery is the skill's main source of fragility: the structured `response_format`
forces `tool_choice="any"` on detectors (root cause of the 68M-token runaway incident),
the merge/marker scripts carry their own failure modes (`skipped` accounting, malformed
archetypes, anchor misalignment), and every change touches multiple reference files.

Anthropic's [pr-review-toolkit](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/pr-review-toolkit)
demonstrates a much simpler shape that works in production: a short orchestrator command
that picks *applicable* agents based on what changed, launches them in parallel, and
aggregates their **prose reports** into a summary. Each agent is a rich, self-contained
persona that self-filters via a confidence score. No schema, no merge script, no parent
verification stage.

This design restructures the skill to that shape while keeping DAIV's five review
dimensions (correctness, security, performance, structure, custom-rules).

## Goals

- Drastically simplify the skill: one orchestrator file, five self-contained detector
  charters, zero scripts.
- Remove the forced structured output on detectors (and with it the `tool_choice="any"`
  footgun).
- Keep review precision ("precision over recall") via detector-side confidence scoring
  plus a light orchestrator skepticism pass.
- Reviews on an MR stack as **multiple reports** (one new discussion per run); re-reviews
  cover **only the diff since the last reviewed commit**.

## Non-goals

- Inline (line-anchored) MR comments — dropped entirely, by decision.
- Aspect-selection arguments (`review security` etc.) — not requested; the applicability
  filter is automatic, not user-driven.
- Making detectors independently invocable outside a review.
- An agent that applies fixes (pr-review-toolkit's `code-simplifier` has no DAIV
  counterpart here; fixes happen through the existing reply-to-DAIV flow).
- Changing when/how the review is triggered (labels, slash commands, webhooks).

## Decisions (from design discussion)

| Question | Decision |
|---|---|
| Motivation | Simplification — current pipeline is too complex/brittle |
| Delivery | Summary-only; **multiple stacking reports**, one per run |
| Re-review scope | Only the diff between last reviewed head and current head |
| Precision | Detector self-filter (confidence ≥ 80) + light parent judgment |
| Charters | Fully self-contained; delete `references/` content files |
| Shared preamble | **Keep** `SHARED_DETECTOR_PREAMBLE` in `subagents.py` (slimmed) |
| Fan-out | Applicability filter before launching (pr-review-toolkit style) |
| Report format | Critical/Important/Suggestions + Questions; no Strengths; no scope/detector info; Recommended Actions + reply-to-DAIV footer |

## Design

### 1. File layout

```
daiv/automation/agent/skills/code-review/
├── SKILL.md                 # the entire orchestrator workflow (~150 lines)
└── agents/
    ├── cr-correctness.md    # rich, fully self-contained personas
    ├── cr-security.md
    ├── cr-performance.md
    ├── cr-structure.md
    └── cr-custom-rules.md
```

**Deleted:**

- `references/review-workflow.md`, `references/gitlab-delivery.md`,
  `references/principles.md`, `references/few-shot-examples.md`,
  `references/marker-format.md`
- `examples/example-review-output.md`
- `scripts/findings.py`, `scripts/marker.py`, `scripts/finding.schema.json`
  (and `__pycache__`)

The five dimensions survive unchanged as the five charters.

### 2. Orchestrator (`SKILL.md`)

One file, no phase references. Workflow:

1. **Mode.** *Delivery mode* when the runtime has `Scope.MERGE_REQUEST` with a
   `merge_request_id`, the platform is GitLab, and the `gitlab` tool loads (via
   `tool_search` if needed); *interactive mode* otherwise. In delivery mode, list the MR
   notes and collect previous review markers
   (`<!-- daiv:code-review run=N head=<sha> -->`) to find the last reviewed head and the
   next run number.

2. **Review scope (incremental).**
   - First review: `git diff <target>...<source>` — the full MR change.
   - Re-review: `git diff <last_head>...<head>`, **filtered to paths also present in the
     full MR diff** (keeps target-branch merge-ins out of scope).
   - If `last_head` is missing locally or is not an ancestor of `head` (force-push /
     rebase): fall back to the full MR diff and state that in the report.
   - If `head` equals the last reviewed head: nothing new to review — do not post;
     reply/return a short "already reviewed at `<sha>`" message.
   - Interactive re-review in the same conversation: scope is what changed since the
     last in-conversation review (conversation context; no markers involved).

3. **Applicability filter (pr-review-toolkit style).** Inspect the scoped diff
   (`--name-only` plus a skim of the hunks) and launch only the detectors that apply:

   | Detector | Runs when |
   |---|---|
   | `cr-correctness` | any code file changed |
   | `cr-structure` | any code file changed |
   | `cr-security` | diff touches trust boundaries: request/input handling, endpoints/views, auth/permissions, secrets or config, SQL/subprocess/file-path construction, dependency manifests, CI/Docker files |
   | `cr-performance` | diff touches loops over collections, DB/ORM queries, network calls, caching, async/concurrency code |
   | `cr-custom-rules` | a rule source exists: `.agents/review-rules.md`, `AGENTS.md`, or `.agents/AGENTS.md` (passed the paths that exist) |

   **Bias to inclusion** — when unsure whether a dimension applies, launch it. If the
   diff contains no code at all (docs/assets only): run only `cr-custom-rules` if rules
   exist, otherwise report that there is nothing applicable to review.

4. **Fan-out.** Write the scoped diff once to `/workspace/tmp/review-change.diff`
   (detectors fall back to running `git diff` themselves if the write fails). Dispatch
   the applicable detectors **in parallel**, one `task` call per detector,
   `subagent_type: cr-*`. The prompt carries **scope only**: source/target refs, head
   SHA, the shared diff path, and the path scope (plus rule-source paths for
   `cr-custom-rules`). Kept rules: never dispatch to `general-purpose`; never restate
   the charter; if an expected `cr-*` type is not offered by the `task` tool, skip it
   and note the gap in the final report body (prose, not a status line). If parallel
   dispatch fails, fall back to sequential; if a detector's `task` errors, continue with
   the rest.

5. **Aggregate.** Read the detector prose reports and apply a light skeptical pass while
   assembling the report — drop findings that are pre-existing (not introduced by this
   diff), style/formatting nits, misread control flow, or on unreachable paths. This is
   a prompt rule, not a pipeline stage. The orchestrator may downgrade a detector's
   severity. Deduplicate overlapping findings across detectors by judgment (same file,
   same line, same underlying issue → keep the strongest framing).

6. **Deliver.**
   - Delivery mode: post the report as **one new top-level MR discussion** via the
     `gitlab` tool. Reports stack — never edit or resolve a previous report. The posted
     report is the deliverable (do not also return the markdown).
   - Interactive mode: return the report as the final assistant message; never post it
     yourself.

Non-negotiables carried over from v3.x: precision over recall; never report style,
formatting, whitespace, or import-ordering findings; detectors run as `cr-*` subagents,
never `general-purpose`; never re-invoke the `skill` tool to restart a review.

### 3. Report format (both modes)

```markdown
<!-- daiv:code-review run=2 head=abc1234def5678 -->
## Code Review #2

### Critical Issues
**1. Sessions never expire when Redis is down** — `daiv/chat/api/relay.py:87`
<details>
<summary>Details</summary>

Rationale, and the concrete fix (fenced code block when it is a code change).

</details>

### Important Issues
…

### Suggestions
…

### Questions
…

### Recommended Actions
1. Fix the session-expiry defect in `daiv/chat/api/relay.py` before merging.
2. …

---
_Reply in this discussion and mention `@<bot-username>` to ask about a finding or have
DAIV apply a fix._
```

Rules:

- The visible header is only `## Code Review #N`. No scope line, no detector counts,
  no merge stats. The run number comes from the markers found in step 1 (interactive
  mode omits the number unless it is a re-review within the conversation).
- The **hidden HTML marker** on the first line carries the state for the next
  incremental run (`run`, `head`). It renders invisibly on GitLab. It is trivial
  embed-and-extract — no script.
- Sections appear only when non-empty; an empty review still posts a short "No findings"
  report so the marker records the reviewed head.
- **Recommended Actions**: ordered list, merge-blocking items first. Omit when there are
  no findings.
- **Footer**: tells the author how to follow up. Verified against the webhook code:
  an MR comment that mentions the bot user (`@<username>`, matched by
  `note_mentions_daiv` in `codebase/utils.py`) or references "DAIV" by name triggers
  `address_mr_comments_task`. The orchestrator uses the real bot username (from runtime
  context / the `gitlab` tool's current user), not a hardcoded handle.
- Findings use `file:line` references; severity buckets are Critical / Important /
  Suggestions; Questions keep their own section and are not severity-graded.
- Force-push fallback (full re-review) is stated in one sentence at the top of the
  report body.

### 4. Detector charters (`agents/cr-*.md`)

Each charter is one self-contained file (~100–150 lines), pr-review-toolkit style:

- **Frontmatter:** `name`, `description` (dispatch-only note, as today), optional
  `model` (loader behavior unchanged).
- **Persona + mission** for the dimension.
- **Investigation process** distilled from the `references/principles.md` sections that
  the detector owns today (e.g. `cr-correctness` absorbs §7, §10, §12, §13, §15,
  §22–§25), plus one or two compressed calibration examples adapted from
  `references/few-shot-examples.md`. The reference files are then deleted; the charters
  are the only home of this content.
- **Confidence scoring 0–100; report only findings ≥ 80.** Replaces the Signal-filter
  bars as the precision gate.
- **Severity rubric** (same text in every charter):
  - **Critical** — wrong results, broken authz, data loss, crash on common inputs.
  - **Important** — likely bug, broken contract, meaningful performance regression.
  - **Suggestion** — concrete structural improvement with a named fix
    ("use X instead of Y", "delete lines L–M", "extract to Z").
  - **Question** — needs the author's intent; must anchor on a `file:line` with a
    concrete yes/no hypothesis.
- **Never-flag rules:** style, formatting, whitespace, import ordering, pre-existing
  issues, lint-suppressed lines, unreachable code paths.
- **Output:** a markdown report — for each finding: severity, `file:line`, one-line
  title, why it is a problem, concrete fix. If nothing clears the confidence bar, the
  literal text "No findings."

The `bar`/`archetype`/JSON-schema system is gone. Severity is assigned by the detector;
the orchestrator may downgrade or drop during aggregation.

`cr-custom-rules` keeps its special input (paths of the rule sources present) and cites
the violated rule in each finding.

### 5. Runtime changes

**`subagents.py`:**

- **Keep** `CODE_REVIEW_DETECTOR_NAMES`, charter loading from `agents/*.md`
  (`load_builtin_code_review_detectors`), the read-only detector middleware stack
  (`_build_detector_middleware` minus deferred output), per-charter `model` frontmatter
  handling, and `LoopBreakerMiddleware`.
- **Keep `SHARED_DETECTOR_PREAMBLE`**, slimmed: it retains the shared procedure (how the
  change is delivered — the diff file path in the prompt with `git diff` fallback — and
  the **read-only contract**, which remains the only enforcement preventing a detector
  from mutating the shared workspace) and drops everything about structured findings,
  the schema, and deferred output. It gains one line: the report shape is defined by
  each charter, and the final message *is* the deliverable returned to the parent.
- **Remove** `_load_detector_response_format` and the finding-schema path constant;
  detectors get no `response_format`, so structured output no longer forces
  `tool_choice="any"` (closes the token-runaway class of failures) and detectors can
  end their run with a normal text message.
- **Remove** `DeferredOutputMiddleware` from the detector stack. Reports return inline
  as the `task` result (they are short by design — self-filtered, prose).

**Middleware:**

- `DeferredOutputMiddleware` has no other users (verified: single construction site in
  `_build_detector_middleware`) → **delete**
  `daiv/automation/agent/middlewares/deferred_output.py` and its tests.
- Update the `loop_breaker.py` docstring that references deferred output for `cr-*`
  detectors. A loop-stopped or errored detector is now simply a failed `task` call; the
  orchestrator mentions the uncovered dimension in the report body.

**Constants:** remove `SUBAGENT_OUTPUT_PATH` if the deferred-output middleware was its
only consumer (verify at implementation time).

### 6. Error handling

| Failure | Behavior |
|---|---|
| `gitlab` tool unavailable / 403 | Demote to interactive mode (unchanged) |
| Shared diff file write fails | Dispatch anyway; detectors run `git diff` themselves |
| A `cr-*` type not offered by `task` | Skip it; note the uncovered dimension in the report body |
| Detector `task` errors / loop-stopped | Continue with the rest; note the gap in the report body |
| Parallel dispatch rejected | Fall back to sequential dispatch |
| All detectors fail | Do not post a "No findings" report — report the failure instead |
| Force-push / unreachable `last_head` | Full re-review; one-sentence notice in the report |
| Head unchanged since last review | Do not post; short "already reviewed" reply |
| Posting the discussion fails | Return the report as the final message with a note that posting failed |

### 7. Tests

- **Delete:** `tests/unit_tests/automation/agent/skills/code_review/test_findings.py`,
  `test_marker.py`, `tests/unit_tests/automation/agent/middlewares/test_deferred_output.py`,
  and the deferred-output–specific assertions in `test_graph_deferred.py` (delete the
  file if that is all it covers).
- **Update:** `test_subagents.py` — detectors compile without `response_format` and
  without deferred-output middleware; charter loading, model frontmatter, skip-on-bad-
  charter behavior all still covered. `test_skills.py` where it touches code-review
  structure.
- **Add:** a test asserting every `agents/cr-*.md` charter contains the required
  sections (severity rubric, confidence gate, output contract) so a charter edit cannot
  silently drop the precision gate — cheap string/heading checks, not semantics.
- Per project convention, tests cover only DAIV's custom logic (loader, middleware
  stack), not prompt content quality.

### 8. Docs & changelog

- Rewrite the **"Code-review detector output"** invariant in `AGENTS.md` (deferred
  JSON pointers → inline prose reports; delete the `findings.py merge` reference).
  Also update the "Skill asset paths" example if it cites `marker.py`.
- Update `docs/reference/agent-architecture.md` (detector pipeline description) and
  `docs/features/pull-request-assistant.md` (review behavior: stacking reports,
  incremental re-reviews, reply-to-DAIV footer). Check `docs/customization/agent-skills.md`
  and `docs/features/subagents.md` for stale references to the old structure.
- `CHANGELOG.md`: notable, user-facing entry — review output format change (summary
  report instead of inline comments), incremental re-reviews, stacking reports.
- Skill `metadata.version` → `4.0.0`.

## Risks & mitigations

- **Precision regression** (no mechanical merge/verify): mitigated by the ≥80 confidence
  gate in every charter, the orchestrator's skeptical pass, and the charter-content
  test. If precision drops in practice, the first lever is charter calibration examples,
  not machinery.
- **Reviewers lose line-anchored comments**: accepted trade-off (explicit decision).
  The report's `file:line` references plus GitLab's file links cover navigation.
- **Marker fragility** (hand-embedded HTML comment): the format is a single line with
  two fields; the orchestrator treats *any* parse failure as "no previous review" and
  falls back to a full review — degraded to correct-but-larger scope, never wrong scope.
- **Stacked reports clutter long-running MRs**: each report is scoped to its delta and
  short by design; accepted in exchange for a readable review history.
- **Read-only contract now lives only in the preamble**: unchanged from today —
  `SHARED_DETECTOR_PREAMBLE` is kept precisely so this stays single-sourced.
