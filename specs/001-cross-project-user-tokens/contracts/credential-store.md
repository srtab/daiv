# Contract: Credential Resolution (internal)

**Consumers**: the two CLI runners in `git_platform.py`, and the account-settings views.
**Owner**: `daiv/accounts/credentials.py`.

The only module permitted to decrypt `PlatformCredential.access_token`. Everything else asks this
service and receives either a usable token or a typed reason it cannot have one.

## Operations

| Operation | Input | Output |
|---|---|---|
| resolve | acting user id, provider, host | A usable access token, or a typed refusal reason |
| status | user, provider | State, expiry, granted scopes — never the secret |
| store | user, provider, host, token response | Persists the grant, `state = connected` |
| revoke | user, provider, host | `state = revoked`, secrets cleared |

## Resolution order

```text
1. capability enabled?          no  ─▶ DISABLED
2. acting user resolvable?      no  ─▶ NO_ACTING_USER
3. credential row exists?       no  ─▶ NO_CREDENTIAL
4. state == connected?          no  ─▶ EXPIRED | REVOKED
5. cached token still valid?    yes ─▶ token
6. expires within 5 minutes?    yes ─▶ refresh (atomic), re-cache, return token
                                fail ─▶ state = expired, EXPIRED
7. return token
```

Each refusal reason maps one-to-one onto a string in
[agent-tools.md](./agent-tools.md#refusal-vocabulary). The service returns the reason; the tool owns
the wording. Reasons are never collapsed into a generic failure — FR-011 requires the person to learn
*which* thing is wrong.

## Invariants

1. **The cache key is the identity**, `(user_id, provider, host)` — never `thread_id`. A resumed
   thread whose acting person differs must not read the previous person's token (FR-013, D6).
2. **The secret never leaves this module** except as the value of a subprocess environment variable.
   Not into a return value a caller might log, not into agent state, not into a checkpoint (FR-012).
3. **Refresh is atomic.** Access token, refresh token, and expiry are written in one transaction.
   GitLab rotates the refresh token on every use; a partial write leaves a credential that can never
   renew again (D5).
4. **Refresh happens at the point of use**, not once per run. A long run can outlive a 2-hour GitLab
   token or an 8-hour GitHub App token — the same failure mode as the known egress-token intra-turn
   expiry. The margin is **5 minutes**: comfortably wider than one call's 30-second CLI timeout plus
   retries, and narrow enough not to refresh on every call. It is a named constant, not a literal at
   the comparison site.
5. **A 401/403 from the platform on a token this service issued** transitions the row to `revoked`,
   so the next call reports the real cause instead of retrying a dead credential.
6. **Never logged.** No token, and no prefix or suffix of one, reaches a log record — including
   exception messages, which is where they usually escape.

## Matching a webhook's triggering user (FR-014)

`resolve_user` ([accounts/utils.py:16](daiv/accounts/utils.py:16)) matches on username, then email,
then social uid. The first two can match a DAIV account that never linked *this* platform identity —
harmless when choosing an MR assignee, its current use, but not when choosing whose credential to
spend.

**Therefore**: credential lookup keys on `(provider, platform_uid)` taken from the event's platform
user, not on the resolved DAIV user alone. A resolved user whose credential row carries a different
`platform_uid` is treated as `NO_CREDENTIAL`, not as a match.
