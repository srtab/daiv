# Code Review — Detector charters

Stage 1 (Detect) dispatches these detectors **in parallel** via the `task` tool (`subagent_type=general-purpose`), one `task` call per detector in a single turn. Each detector owns a focused slice of the review, reads surrounding code for context before deciding (context is what keeps false positives down), and returns a **JSON array of findings and nothing else** in the shared schema (see SKILL.md → Finding schema). The rationale behind every category lives in `references/principles.md` — open the cited section when a finding's framing is unclear; do not restate it.

A finding only belongs in the array if it meets one of the Signal-filter bars (defect / structural concern / question — see SKILL.md → Stage 2). Never flag style, formatting, whitespace, or import ordering — tooling handles those. Naming is flagged only when it materially misleads.

## `correctness`

Logic and contract defects. Owns `principles.md` §7 (correctness defect), §10 (configuration/environment), §12 (fail-fast vs defensive), §13 (unintended side effects), §15 (absent-value handling), §22 (concurrency/locking), §23 (error handling), §24 (migrations/schema changes), §25 (API contract / backward compatibility). Typical findings: clearly wrong logic, a removed/renamed column or endpoint still read by deployed code, a non-nullable column added without a default, a swallowed error, a hook now firing where it didn't.

## `security`

Trust-boundary and exposure issues. Owns §14 (input validation), §18 (authorization/authentication), §19 (secrets exposure). Typical findings: unvalidated external input reaching business logic, an authz check missing on a mutation, a secret in code/logs.

## `performance`

Owns §16 (performance — general) and §17 (repeated queries/lookups in loops). Typical findings: an N+1 query, a remote call or cache/filesystem lookup inside a loop that one batched call before the loop would replace, an O(n²) over user-controlled input.

## `structure`

Maintainability and readability. Owns §1 (dead code), §2 (wrong placement/responsibility), §3 (use existing framework/library feature), §4 (naming that misleads), §5 (duplication/reuse), §6 (convention deviation), §8 (i18n), §9 (UI/UX/accessibility), §11 (magic values), §20 (typing/signatures), §21 (logging/observability). Typical findings: the five high-value patterns (dead lines, unused framework idioms, misplaced logic, missed reuse, misleading naming) plus magic literals, lying signatures, unstructured logs.

## `custom-rules`

**Dispatched when any rule source exists** (Stage 0: `.agents/review-rules.md`, `AGENTS.md`, or `.agents/AGENTS.md`). The parent passes the **paths** of the ones present, not their contents — read them yourself: `.agents/review-rules.md` is authoritative (binding); `AGENTS.md` / `.agents/AGENTS.md` are supplementary — mine them only for concrete, diff-checkable rules (naming, layering/boundaries, required/forbidden patterns) and ignore build/test/setup prose and vague aspirational lines. Every finding **must** set `source` to the rule it enforces (e.g. `review-rules.md: every external call in payments/ must set a timeout`) so the posted comment can cite it. Custom-rule findings are not exempt from Stage 2 — they are refuted and bar-filtered like any other.

§26 (question for the author) is not owned by a detector: any detector may emit a `bar: "question"` finding when the issue needs the author's intent rather than a fix — e.g. a missing test for a non-trivial new code path (ask whether it was intentionally skipped), the test-coverage angle the skill advertises.

When emitting a finding, set `archetype` to one of the six schema values only (the four inline fix types, `question`, or `discussion`). The discussion-only patterns named in `references/few-shot-examples.md` are documentation labels — serialize them all as `archetype: "discussion"`.
