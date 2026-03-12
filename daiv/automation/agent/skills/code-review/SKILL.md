---
name: code-review
version: 1.0.0
description: This skill should be used when a user asks for a code review, feedback on a PR or MR, diff assessment, or says things like 'can you review my changes', 'look at this diff', 'is this ready to merge', 'check my code', 'review this branch', 'what do you think of these changes', or 'LGTM check'. Covers correctness, tests, performance, security, and architecture feedback on pull/merge requests or raw diffs from any platform (GitHub, GitLab).
---

# Code Review

## Establish scope and inputs

- Identify whether the request targets a merge/pull request, a local diff, or specific files.
- If a merge/pull request is referenced:
  1. fetch merge/pull request to determine the source branch and target branch using the available tools;
  2. fetch the diffs using `bash git diff <target>...<source>` to review what the source branch introduces. If bash fails, fall back to the platform tool instead of retrying bash;
- If a diff is already provided, review that directly without re-fetching.
- If the scope is ambiguous, infer it from the conversation history and available artifacts.
- Otherwise, ask the user to provide more context.

## Error recovery

- If a tool call fails, switch to an alternative tool (e.g., platform tool instead of `bash git diff`) and continue from where you are. Never re-invoke the `skill` tool to restart the review.

## Review checklist

- Validate correctness, edge cases, and error handling.
- Confirm adherence to project conventions and architecture.
- Check performance implications or scalability risks.
- Evaluate tests: coverage for new/changed behavior, missing tests, or flaky patterns.
- Highlight security considerations (input validation, authz/authn, secrets, data handling).
- Note documentation or changelog impacts when user-facing behavior changes.

## Signal filter

Before writing any finding, verify it meets at least one of these criteria:

- **Certain**: The code will fail to compile/parse, or will definitely produce wrong results regardless of inputs.
- **High-confidence**: The code has a clear defect that affects common paths — resource leaks, null/None dereferences on values that are obviously nullable, missing error propagation, race conditions with a concrete trigger scenario.

A finding is certain if you could write a failing test without knowing the runtime environment. A finding is high-confidence if you can describe a plausible, non-contrived scenario that triggers it.

Do not flag style concerns, subjective improvements, or issues that require exotic inputs or unusual state to manifest. False positives erode trust and waste reviewer time.

If you reconsider a finding during analysis and conclude it is not a real issue, drop it entirely. Never include self-corrected findings, strikethrough text, or "on closer reading this is fine" in the output. Reason internally, present only confirmed findings.

## Response format

**CRITICAL: Do NOT post the review as a comment, note, or discussion on any issue or merge request. The harness handles delivery — your job is to return the review as your final output message only.** Do not add preamble text like "Here is the review:".

Use the link format from the "Code References" section in the system prompt for all file locations. Place the file reference in the finding summary line, not in the body.

For a complete example of a well-formed review, see `examples/example-review-output.md`.

### Findings

Numbered list grouped by severity (High/Medium/Low). Each finding has a **one-line summary** with the file reference, and a collapsible `<details>` block for the full explanation and fix.

```
**1. Summary of the issue** — [path/to/file.py:42](link)

<details>
<summary>Details</summary>

Explanation and fix.

</details>
```

If there are no findings, write "No findings." and skip the section.

### Suggestions

Optional non-blocking improvements as a bullet list. Keep each to one sentence. Omit if none.

### Tests

Bullet list of specific missing test cases. Omit if coverage is adequate.
