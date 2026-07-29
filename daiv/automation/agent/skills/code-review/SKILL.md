---
name: code-review
description: This skill should be used when the user asks for a code review of a change — a pull request, merge request, branch, commit range, working-tree change, or pasted diff — covering correctness, security, performance, structure, and repository-specific rules. Trigger on phrases like "review this PR", "review this MR", "code review", "review my changes", "review this diff", "look over this branch", "is this ready to merge", "any issues with these changes", "merge-readiness check", or "give me feedback on this change", for GitHub, GitLab, or a local repository. For a security-only deep audit of a feature or codebase area rather than a multi-dimension review of a specific change, use the `security-audit` skill instead.
---

# Code Review

Review a change with specialized `cr-*` detector subagents, then combine their reports into one concise review returned as your final message.

The final message is the only deliverable. Never create a platform discussion, review, comment, or note yourself. When the request came from a pull-request or merge-request comment, the platform layer posts your final message automatically.

## 1. Resolve the review context

Use the same workflow for GitHub pull requests and GitLab merge requests.

- **Platform review:** the request arrived from a PR or MR comment. Use the platform context and read tools to identify the change and previous DAIV reviews. Include the review marker, run number, and footer in the final message.
- **Interactive review:** any chat or CLI request, including a PR or MR the user merely pointed at, a local change, or a pasted diff. Return the report directly without a marker or footer.

Choose the review scope in this order:

1. Use an explicit diff, commit range, branch range, path set, PR, or MR named by the user.
2. Treat a pasted diff as the authoritative change when repository refs are unavailable.
3. For a platform review, use the PR or MR base and head.
4. Otherwise review the current working-tree change, including staged, unstaged, and new files.

Ask when the intended scope is ambiguous. Resolve refs to commit SHAs before using them in shell commands.

## 2. Find the previous platform review

For a platform review, load the appropriate GitHub or GitLab read tool and list all comments or notes, following pagination. Do not use any write action.

DAIV review comments contain:

```text
<!-- daiv:code-review run=N head=<full-sha> -->
## Code Review #N
```

A marker counts only when:

- the comment was authored by DAIV's account;
- it matches the marker grammar exactly, with a 40-character hexadecimal head SHA;
- the comment contains at least one review section or a `No findings` result.

Ignore every other marker. The next run number is the highest valid run plus one. The previous reviewed head is the head from the highest valid run.

- With no valid previous review, review the full current change as run 1.
- If comments cannot be read, review the full current change as run 1 and mention that previous reviews could not be checked.
- If the current head equals the previous reviewed head, return `Already reviewed at <short-sha> — no new commits since review #N.` and stop without a marker.
- Otherwise review only the changes since the previous reviewed head, restricted to paths in the current PR or MR.
- If the previous head is missing, is not an ancestor of the current head, or cannot produce a trustworthy incremental diff, review the full current change and mention the fallback.

## 3. Prepare one canonical diff

Create one canonical diff for the resolved scope, write it to the run scratchpad at `/workspace/tmp/review-change.diff`, and retain its line count. All detectors must review that same change.

For platform reviews:

- First review: use the full base-to-head PR or MR diff.
- Re-review: use the previous reviewed head to current head range, restricted to paths in the current PR or MR.

For interactive reviews, materialize the exact explicit, pasted, commit-range, branch, or working-tree change selected in Step 1.

If shell access is unavailable, obtain the changed hunks from the platform read tool and provide that canonical diff to the detectors. Do not substitute complete new-side files: that would include pre-existing code outside the review scope. If no changed-hunk diff can be obtained, explain that the review could not be completed and stop.

Preserve paths without shell interpolation; use NUL-delimited changed-path lists when supported so spaces, renames, and unusual filenames remain intact.

Do not load a large full diff into the parent context. Build a concise applicability summary from:

- the PR or MR title and description, when available;
- changed paths and file statuses;
- the diffstat;
- hunk headers and a small changed-code preview only when needed.

## 4. Select and dispatch detectors

Select applicable detectors from the concise summary. Do not use keyword regexes over the diff. Identify the kinds of files and changes involved, then run every plausibly relevant reviewer.

| Detector | Dispatch when |
|---|---|
| `cr-correctness` | behavior, source, tests, configuration, schemas, migrations, dependencies, CI, or infrastructure changed |
| `cr-structure` | source structure, public interfaces, types, modules, data models, or multi-file design changed |
| `cr-security` | authentication, authorization, input, trust boundaries, secrets, dependencies, configuration, CI, deployment, file access, commands, or data exposure may be affected |
| `cr-performance` | database access, collections, loops, network or file I/O, caching, serialization, async work, concurrency, or resource use may be affected |
| `cr-custom-rules` | `.agents/review-rules.md`, `AGENTS.md`, or `.agents/AGENTS.md` exists; pass the existing rule-source paths as absolute |

Bias toward inclusion when applicability is uncertain. For documentation or asset-only changes, run only `cr-custom-rules` when rules exist; otherwise report that nothing applicable changed.

Dispatch applicable detectors in parallel when supported, with one task per `cr-*` subagent. Fall back to sequential dispatch if necessary.

Each detector prompt contains only:

- the resolved ref range as commit SHAs (`<base-sha>..<head-sha>`), or an explicit `working tree` or `pasted diff` when the scope has no range;
- the canonical diff path and line count, or the canonical diff content when no file is available;
- the head SHA when available;
- the new-side path scope: the changed paths;
- the rule-source paths for `cr-custom-rules`.

The line count is a completeness aid. Detectors may inspect a large diff in bounded chunks; do not require one tool call to return the entire file.

Do not restate the detector charter or output format. Never substitute a missing `cr-*` detector with `general-purpose` or another agent type.

## 5. Aggregate the reports

Treat a detector as unavailable when its task errors, returns empty output, opens with `ERROR:`, or contains neither recognizable findings or questions nor `No findings.` Continue with every usable report.

A recognizable entry is a well-formed `### Critical:`, `### Important:`, `### Suggestion:`, or `### Question:` entry defined by the detector charters.

Treat `No findings.` as a clean report. Ignore unrelated narration around otherwise recognizable entries.

Act as an aggregator, not a second reviewer. Detectors own investigation, evidence, and confidence. Do not re-read source or re-derive control flow to verify their conclusions.

Aggregate by:

- dropping findings with confidence below 80, no changed-side location, malformed evidence, or generic style, formatting, whitespace, or import-order nits;
- preserving a style-related finding when `cr-custom-rules` cites an explicit repository rule requiring it;
- deduplicating findings with the same underlying issue;
- keeping the higher detector-provided severity when duplicates disagree;
- keeping relevant questions separate from findings; questions do not require a confidence score or location, although they should include a location when one is applicable;
- removing internal `Confidence` and `Verify` fields.

Keep a `cr-custom-rules` finding's `Rule:` citation. If one or more applicable detectors were unavailable, briefly name those dimensions in the final report. If every applicable detector was unavailable, explain that the review could not be completed and return no marker.

## 6. Return the report

For a platform review, the marker must be the first line:

```markdown
<!-- daiv:code-review run=N head=<full-sha> -->
## Code Review #N

_Review unavailable for: <dimensions>._ <!-- omit when all applicable detectors returned usable reports -->

### Critical Issues
**1. <one-line title>** — [`path/to/file.py:42`](<blob-link>)

<details>
<summary>Details</summary>

**Rule:** `review-rules.md: <rule>` <!-- custom-rule findings only -->

Why this is a problem, followed by a concrete fix.

</details>

### Important Issues
...

### Suggestions
...

### Questions
...

### Recommended Actions
1. <merge-blocking actions first, followed by the remaining important actions>

---
_Reply to this comment and mention `@<bot-username>` to ask about a finding or have DAIV apply a fix._
```

Report rules:

- Omit empty sections.
- Number entries sequentially within each section.
- Include Recommended Actions only when Critical or Important findings exist.
- When all applicable detectors return `No findings.`, use `No findings — the reviewed changes look good.`
- When no findings survive and some detectors were unavailable, use `No findings — none confirmed by the detectors that completed.` Keep the `No findings` prefix: Step 2 treats a comment without it as an invalid marker and re-reviews the whole change.
- When nothing applies, use `No findings — nothing applicable to review in this change.`
- Add one short italic sentence under the header for an unreadable review history or a full-diff fallback.
- Link locations to platform blobs when possible; otherwise use plain `path:line` references.
- Use DAIV's actual account username from the runtime context.
- In interactive mode, use `## Code Review`; omit the marker, run number, and footer.

## Non-negotiables

- Return the report only as the final message.
- Never create or post a discussion, review, comment, or note through a GitHub or GitLab tool.
- Never restart the skill after a tool or detector failure; continue with the usable results.
- In platform reviews, emit nothing before the marker.
