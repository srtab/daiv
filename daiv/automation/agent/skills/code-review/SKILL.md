---
name: code-review
description: This skill should be used when a user asks for a code review, feedback on a PR or MR, diff assessment, or says things like 'can you review my changes', 'look at this diff', 'is this ready to merge', or 'LGTM check'. Covers correctness, style, tests, performance, security, and architecture feedback on pull/merge requests or raw diffs from any platform (GitHub, GitLab).
---

# Code Review

## Establish scope and inputs

- Identify whether the request targets a merge/pull request, a local diff, or specific files.
- If a merge/pull request is referenced:
  1. fetch merge/pull request to determine the source branch and target branch using the available tools;
  2. fetch the diffs between source branch and target branch to review the changes;
- If a diff is already provided, review that directly without re-fetching.
- If the scope is ambiguous, infer it from the conversation history and available artifacts.
- Otherwise, ask the user to provide more context.

## Review checklist

- Validate correctness, edge cases, and error handling.
- Confirm adherence to project conventions and architecture.
- Check performance implications or scalability risks.
- Evaluate tests: coverage for new/changed behavior, missing tests, or flaky patterns.
- Highlight security considerations (input validation, authz/authn, secrets, data handling).
- Note documentation or changelog impacts when user-facing behavior changes.

## Signal filter

Before writing any finding, verify it meets at least one of these criteria:

- The code will fail to compile or parse (syntax error, missing import, unresolved reference).
- The code will definitely produce wrong results regardless of inputs — not a hypothetical, not input-dependent.

A finding is certain if you could write a failing test or reproduce the error without knowing anything about the runtime environment or caller behavior. If you cannot meet that bar, omit it.

Do not flag style concerns, subjective improvements, or issues that only manifest under specific inputs or state. False positives erode trust and waste reviewer time.

## Response format

- **Overview**: 1-3 bullets on what changed.
- **Findings**: numbered list grouped by severity (High/Medium/Low) with actionable fixes, so users can reference findings by number (e.g. "fix #3").
- **Suggestions**: optional improvements that are not blocking.
- **Tests**: Note which tests exist for changed code, identify gaps, and suggest specific test cases.
