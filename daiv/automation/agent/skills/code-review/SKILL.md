---
name: code-review
description: Review code changes and provide structured feedback for merge/pull requests or diffs. Use when asked to review PR/MR changes, assess correctness, style, tests, performance, or security, and return actionable review notes.
---

# Code Review

## Establish scope and inputs

- Identify whether the request targets a merge/pull request, a local diff, or specific files.
- If a merge/pull request is referenced and the git platform tool is available, fetch context and diffs before reviewing:
  - `gitlab("project-merge-request get --iid <merge_request_iid>", output_mode="detailed")`
  - `gitlab("project-merge-request-diff list --mr-iid <merge_request_iid>")`
  - `gitlab("project-merge-request-diff get --mr-iid <merge_request_iid> --id <diff_id>")`
- If a diff is already provided, review that directly without re-fetching.
- If the scope is ambiguous, infer it from the conversation history and available artifacts.

## Review checklist

- Validate correctness, edge cases, and error handling.
- Confirm adherence to project conventions and architecture.
- Check performance implications or scalability risks.
- Evaluate tests: coverage for new/changed behavior, missing tests, or flaky patterns.
- Highlight security considerations (input validation, authz/authn, secrets, data handling).
- Note documentation or changelog impacts when user-facing behavior changes.

## Response format

- **Overview**: 1-3 bullets on what changed.
- **Findings**: concise bullets grouped by severity (High/Medium/Low) with actionable fixes.
- **Suggestions**: optional improvements that are not blocking.
- **Tests**: what was run, what should be run, or gaps to cover.
