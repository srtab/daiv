---
name: security-audit
description: This skill should be used when the user asks to review code for security vulnerabilities, audit a pull request or merge request for risks, check if code is safe, find injection flaws or hardcoded secrets, or assess the security posture of a feature or codebase area. Use this skill even when the user doesn't say "security audit" explicitly — trigger on phrases like "is this code safe?", "audit this PR", "check for vulnerabilities", "any security issues here?", "review my changes for risks", "are there hardcoded secrets?", "check for SQL injection", "security review", or "assess the risk in this diff". When in doubt, use this skill — it's better to over-trigger on security reviews than to miss one.
---

# Security Audit

## 1. Establish Scope

Determine the audit target before proceeding:

**If a pull request or merge request is referenced:**
1. Fetch the PR/MR to identify source and target branches using the available tools;
2. Fetch the diff between branches.
3. Identify any files changed adjacent to the diff that may affect security (e.g., middleware, auth decorators, shared utilities).

**If a diff, file list, or code snippet is already in context:**
Proceed directly — no need to re-fetch.

**If scope is ambiguous:**
Infer from conversation history. If still unclear, ask one focused scoping question before proceeding.

Scope the audit to changed code paths plus any critical adjacent components (auth layers, input boundaries, data access objects, encryption utilities).

## Error Recovery

If a tool call fails, switch to an alternative tool (e.g., platform tool instead of `bash git diff`) and continue from where you are. Never re-invoke the `skill` tool to restart the audit.

---

## 2. Run the Audit

Work through each category in the checklist. For each category, note any findings before moving on. Skip categories clearly out of scope (e.g., no database code → skip SQLi).

For full detection heuristics per category, see → `references/checklist.md`

**Checklist categories:**
- Authentication & authorization (privilege boundaries, missing checks)
- Input validation & injection (SQLi, XSS, command injection, SSRF, path traversal)
- Secrets management (hardcoded tokens, credentials in logs or error messages)
- Data protection (encryption at rest/in transit, PII handling, unnecessary data retention)
- Dependency & supply-chain risks (unpinned, outdated, or suspicious packages)
- Error handling (stack traces or sensitive data in responses)
- Cryptography (weak algorithms, insecure randomness, key management)
- API security (missing auth on endpoints, CORS misconfiguration, rate limiting)

---

## 3. Apply the Signal Filter

Before writing any finding, verify it clears this bar:

**A finding is valid if you can describe a plausible attacker action that produces a real impact.**

That means you can answer all three:
1. **Who** — what attacker position is required? (unauthenticated, authenticated user, admin, network-adjacent)
2. **How** — what specific action do they take? (craft a request, supply a payload, chain with another finding)
3. **Impact** — what do they gain? (read data, modify data, execute code, deny service)

If you cannot concretely answer all three, omit the finding or demote it to a Recommendation.

**Do not flag:**
- Theoretical weaknesses with no realistic exploit path in this architecture (e.g., "MD5 is weak" when the hash protects non-sensitive data and no attacker can obtain the hash)
- Missing hardening headers on APIs that are never called from a browser
- Style concerns or best practices with no security consequence
- Issues fully mitigated by a compensating control already present in the diff or codebase
- Speculative supply-chain risks on dependencies with no known CVE

**The test:** Could you write a proof-of-concept — even a short one — that demonstrates the issue? If not, it's not a finding. False positives erode trust and cause real vulnerabilities to be buried in noise.

If you reconsider a finding during analysis and conclude it is not valid, drop it entirely. Never include self-corrected findings, strikethrough text, or "on closer reading this is fine" in the output.

---

## 4. Classify Findings

Assign each finding a severity level before writing the report.

For full severity criteria and calibration examples, see → `references/severity-definitions.md`

> Note: only findings that passed the signal filter in Step 3 should appear here.

| Severity | One-line rule |
|----------|---------------|
| Critical | Exploitable now, direct impact (RCE, auth bypass, exposed secrets) |
| High     | High-confidence risk requiring active exploitation or chaining |
| Medium   | Real risk requiring specific conditions or non-trivial effort |
| Low      | Defense-in-depth, minor hardening, best practice gap |

---

## 5. Verify Critical and High Findings

Before writing the report, verify all Critical and High findings. Launch a single verification subagent using the `task` tool with the following mandate:

- Provide it with the list of Critical and High findings (title, claimed location, vulnerability type).
- Instruct it to read the actual source files at the claimed locations and confirm or reject each finding.
- A finding is **confirmed** if the code at the stated location matches the claimed vulnerability.
- A finding is **rejected** if the code doesn't match, the vulnerability is already mitigated, or the location is wrong.

Drop rejected findings entirely. Medium and Low findings do not require this step — the signal filter in Step 3 is sufficient.

---

## 6. Write the Report

**CRITICAL: Do NOT post the report as a comment, note, or discussion on any issue or merge request. The harness handles delivery — your job is to return the report as your final output message only.**

Use the link format from the "Code References" section in the system prompt for all file locations.

When the same vulnerability pattern appears across multiple files, group them into a single finding with a locations table rather than creating separate findings per file.

Structure every audit report in this order:

**Summary** (2–4 bullets)
Overall security posture, count of findings by severity, highest-risk area.

**Findings** (one block per finding)
Group by severity descending. Each finding has a **one-line summary** with ID, title, and location, and a collapsible `<details>` block containing the risk, evidence, and remediation. This keeps the report scannable.

```
**C-01 — Title of the finding** — `path/to/file.py:42`

<details>
<summary>Details</summary>

**Risk:** What an attacker could do and under what conditions.

**Evidence:**
(vulnerable code snippet)

**Remediation:**
Concrete fix with example code where helpful.

</details>
```

**Recommendations** (non-blocking)
Improvements that reduce attack surface but aren't findings — missing headers, missing logging, hardening opportunities. This is also where filtered-out near-findings belong: briefly note what was considered and why it didn't meet the bar, so reviewers know the area was examined.

**Testing & Validation**
Security tests to add or missing coverage areas relevant to the diff.

For a complete example of a well-formed report, see → `examples/example-audit-output.md`
