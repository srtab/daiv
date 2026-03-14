# Severity Definitions & Calibration

Use this file to assign consistent severity levels to findings. When in doubt between two levels, consider: how much attacker effort is required, and how much business impact does exploitation have?

---

## Critical

**Criteria — ALL of the following:**
- Exploitable without special conditions or victim interaction
- Direct, significant impact (data breach, full account takeover, remote code execution, service destruction)
- No compensating controls required to be absent

**Examples:**
- SQL injection that returns all user records unauthenticated
- Hardcoded AWS secret key with broad IAM permissions committed to the repo
- Authentication bypass allowing any user to log in as any other user
- Unprotected admin endpoint with no auth check
- Remote code execution via unsanitized `eval()` on user input
- Exposed private key or certificate in source

**Report tone:** Immediate action required. Do not ship.

---

## High

**Criteria:**
- Exploitable under realistic conditions but requires some attacker effort (e.g., must be logged in, must know a valid ID, must chain with another issue)
- Significant impact on confidentiality, integrity, or availability
- No special privileges required beyond normal user access

**Examples:**
- Insecure direct object reference (IDOR): authenticated user can access other users' records by changing an ID parameter
- Stored XSS in a field rendered to other users (e.g., admin dashboard)
- JWT accepted with `alg: none` (requires crafting a malicious token)
- Password reset tokens that don't expire and are reused
- SSRF to internal metadata endpoint (e.g., `169.254.169.254`)
- Missing rate limiting on login endpoint (enables credential stuffing at scale)

**Report tone:** Fix before release or in the next sprint.

---

## Medium

**Criteria:**
- Real risk, but requires specific conditions: user interaction, specific environment, or chaining with another issue to be impactful
- Moderate impact: partial data exposure, limited privilege escalation, denial of service for individual users

**Examples:**
- Reflected XSS requiring victim to click a crafted link
- Username enumeration via different error messages ("user not found" vs "wrong password")
- Weak password hashing (bcrypt with cost factor 4 instead of 12)
- Missing `HttpOnly` or `Secure` flags on session cookie
- Stack traces returned in production error responses
- CORS misconfiguration on non-credentialed endpoints
- Dependency with a known CVE that requires local access to exploit

**Report tone:** Schedule fix. Include in next security-focused sprint.

---

## Low

**Criteria:**
- Defense-in-depth issue; exploitation requires unusual conditions or an already-compromised component
- Low direct impact; primarily a hardening opportunity or best practice gap
- No realistic attack path in the current architecture

**Examples:**
- Missing `Content-Security-Policy` or `X-Frame-Options` headers
- Using `random` instead of `secrets` for non-security-critical token generation
- Dependency slightly out of date but no known CVEs
- Verbose error logging that includes internal paths (but not user data)
- `console.log` of non-sensitive request data in production
- HTTP used for internal service-to-service calls within a private VPC

**Report tone:** Recommend fixing. Good hygiene; prioritize if other work is slow.

---

## Calibration Reference

| Scenario | Wrong call | Correct call | Reason |
|---|---|---|---|
| Hardcoded Slack webhook URL | Medium | High/Critical | Webhooks can post to channels; impact depends on what the webhook can do |
| Missing CSRF on a read-only GET endpoint | Medium | Low | CSRF only matters for state-changing operations |
| `eval(user_input)` behind admin auth | Critical | High | Admin-only reduces realistic attacker pool |
| MD5 for password hashing | Critical | High | Requires obtaining the hash first; no direct exploit path |
| `verify=False` on internal-only HTTPS call | Medium | Low | No attacker can MITM internal traffic in most architectures |
| IDOR in a multi-tenant SaaS | Low | High | Tenant isolation is a primary security guarantee |
| "MD5 is a weak algorithm" (hash protects non-sensitive data, hash not exposed) | Low | **Omit** | Cannot answer Who/How/Impact — no realistic attacker path; belongs in Recommendations at most |
| Missing `X-Frame-Options` on a JSON API endpoint | Low | **Omit** | Clickjacking requires a browser rendering the response as a page; doesn't apply |

---

## Downgrading & Upgrading

**Reasons to downgrade one level:**
- Requires admin or high-privilege account to exploit
- Only exploitable in a dev/staging environment, not production
- A compensating control exists (WAF rule, network restriction, other validation layer)

**Reasons to upgrade one level:**
- Finding is part of an obvious exploit chain with another finding in this audit
- The affected feature handles particularly sensitive data (healthcare, financial, auth)
- The codebase has no monitoring or alerting (exploitation would go undetected)
