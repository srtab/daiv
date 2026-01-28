---
name: security-audit
description: Perform a security audit of code changes or related code paths. Use when asked to assess security risks in a PR/MR, issue, or feature area and report findings with severity and remediation.
---

# Security Audit

## Establish scope and inputs

- Determine whether the audit targets a merge/pull request, specific files, or a broader codebase area.
- If a merge/pull request is referenced and the git platform tool is available, fetch context and diffs before auditing:
  - `gitlab("project-merge-request get --iid <merge_request_iid>", output_mode="detailed")`
  - `gitlab("project-merge-request-diff list --mr-iid <merge_request_iid>")`
  - `gitlab("project-merge-request-diff get --mr-iid <merge_request_iid> --id <diff_id>")`
- If a diff or file list is already provided, proceed without re-fetching.
- Scope the audit to the affected code paths and any critical adjacent components.

## Audit checklist

- Authentication and authorization correctness, including privilege boundaries.
- Input validation and injection risks (SQLi, XSS, command injection, SSRF).
- Secrets management (hardcoded tokens, leaked credentials, unsafe logging).
- Data protection (encryption at rest/in transit, PII handling, data minimization).
- Dependency and supply-chain risks (unsafe or outdated libraries).
- Error handling that may leak sensitive details.
- Cryptography usage (weak algorithms, insecure randomness, misuse).
- API security (rate limiting, CORS, authentication on endpoints).

## Response format

- **Summary**: 1-3 bullets on overall posture and hotspots.
- **Findings**: group by severity (Critical/High/Medium/Low) with clear remediation.
- **Recommendations**: non-blocking improvements and follow-ups.
- **Tests/Validation**: security tests to run or missing coverage.
