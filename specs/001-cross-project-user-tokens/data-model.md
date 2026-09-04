# Phase 1 Data Model: Cross-Project Git Platform Access

**Date**: 2026-09-03 | **Plan**: [plan.md](./plan.md) | **Research**: [research.md](./research.md)

Two new persisted entities, one new settings field, and two runtime-only values. Existing models that
the feature *reads* are listed at the end so `/speckit-tasks` does not mistake them for new work.

---

## PlatformCredential (new)

`daiv/accounts/models.py`. The per-person OAuth grant DAIV acts with beyond the attached project.
Spec entity: *Platform authorisation*.

| Field | Type | Notes |
|---|---|---|
| `user` | FK → `accounts.User`, `CASCADE` | Deleting a user destroys the credential. |
| `provider` | char, `GitPlatform` choices | `gitlab` or `github`. |
| `host` | char | Platform origin the grant is valid for. Distinguishes a self-hosted GitLab from gitlab.com and makes the "never target the wrong host" edge case checkable. |
| `platform_uid` | char | The platform's own user id, copied from the linked `SocialAccount.uid`. **The credential is matched on this**, not on the DAIV user alone — see D7. |
| `access_token` | encrypted text | Fernet descriptor, `_access_token_encrypted` column, per `core/models.py`. Never returned to a caller that is not the credential service. |
| `refresh_token` | encrypted text, nullable | Null when the platform issues non-expiring tokens (GitHub App with expiry disabled). |
| `expires_at` | datetime, nullable | Null ⇒ non-expiring. |
| `scopes` | JSON list | What was actually granted — may be narrower than requested. Drives the "insufficient" half of FR-011. |
| `state` | char enum | `connected` / `expired` / `revoked`. See transitions below. |
| `created`, `modified` | datetime | `TimeStampedModel`, as `APIKey` does. |

**Constraints**
- Unique on `(user, provider, host)` — one live grant per person per platform host.
- Index on `(provider, platform_uid)` — the lookup FR-014 performs for a webhook's triggering user.

**Validation rules**
- `refresh_token` null **and** `expires_at` non-null is invalid: an expiring credential with no way to
  renew is a credential that will silently die. Reject at write time rather than discovering it at
  the point of use.
- `state = connected` requires a non-empty `access_token`.
- `scopes` is recorded from the token response, never assumed from what was requested.

**State transitions**

```text
(absent) ──sign-in consent──▶ connected
connected ──expires_at passes, refresh succeeds──▶ connected   (tokens rotated in one transaction)
connected ──expires_at passes, refresh fails─────▶ expired
connected ──user disconnects in DAIV─────────────▶ revoked  (secrets cleared)
connected ──platform-side revocation, seen as 401 on use──▶ revoked  (secrets cleared)
expired | revoked ──re-consent──▶ connected
```

`expired` and `revoked` are distinct because FR-011 must name the cause: `expired` tells the person
to re-authorise, `revoked` tells them the grant was withdrawn. Both clear the stored secrets — a row
in either state holds no usable token, only the fact that it once did.

**Refresh is a single transaction.** GitLab rotates the refresh token on every use (D5): writing the
new access token without the new refresh token leaves a credential that cannot renew again. Access
token, refresh token, and expiry are written together or not at all.

---

## CrossProjectAccessRecord (new)

`daiv/codebase/models.py`. The auditable trace FR-016 requires and SC-007 reads.
Spec entity: *Access record*.

| Field | Type | Notes |
|---|---|---|
| `occurred_at` | datetime, indexed | |
| `thread_id` | char, indexed | Ties the record to the run and its `Activity` row. |
| `acting_user` | FK → `accounts.User`, `SET_NULL`, nullable | Null when no acting person could be resolved. |
| `identity_kind` | char enum | `user` or `service`. Makes "which of the two identities acted" answerable without joining. |
| `provider` | char | |
| `target_repo_id` | char, indexed | The project actually targeted. |
| `outcome` | char enum | `allowed` / `denied_no_access` / `denied_no_credential` / `denied_disabled` / `error`. |

**Written per call, not per run** — two identities coexist within one run, so a per-run row cannot
answer SC-007.

**What it must not contain**: no token, no fragment of a token, and no content fetched from the target
project. The record proves *that* a project was reached, never *what* was in it — otherwise the audit
trail becomes a second copy of the data FR-005 protects.

**Retention**: rows accumulate per tool call. Pruning is an operator concern; treat it the way
`RepositoryAccess` treats staleness rather than growing unbounded.

---

## SiteConfiguration.cross_project_access_enabled (new field)

`daiv/core/models.py`. Boolean, default **`False`**, following `web_search_enabled` and
`auth_login_enabled`. Satisfies FR-019.

Default-off is the deliberate choice: enabling it changes what the agent can reach, and existing
deployments must not acquire that reach by upgrading. Off ⇒ the `project` argument is absent from the
tool schema and the cross-project branch is unreachable.

---

## Runtime-only values (not persisted)

**Acting person** (spec entity) — `RuntimeCtx.acting_user_id`, already present at
[context.py:92](daiv/codebase/context.py:92). This feature makes it load-bearing rather than
MCP-selection-only, and fills it in for webhook runs (FR-014, D7). Its docstring must be corrected in
the same change: "`None` for webhook-triggered runs" stops being true.

**Cached access token** — Redis, keyed on `(user_id, provider, host)`, TTL bounded by `expires_at`.
Explicitly **not** `thread_id`-keyed: that shape would serve one person's token to another on a
resumed thread (FR-013, D6). Never written into `GitPlatformState` and never checkpointed (FR-012).

**Target project** (spec entity) — the validated `project` argument, or the attached repository when
empty. Lives only for the duration of one tool call.

---

## Existing models this feature reads (no change)

| Model | Role here |
|---|---|
| `allauth SocialAccount` | Source of `platform_uid` and the existing account link. Not modified; `SOCIALACCOUNT_STORE_TOKENS` stays `False` (D2). |
| `codebase.RepositoryAccess` | **Not consulted for cross-project decisions** (D8) — its universe is bot-visible repos, so it would deny the very projects this feature exists to reach. Continues to serve dashboard authorization unchanged. |
| `codebase.RepositoryCatalog` | Unchanged. |
| `Session` / run rows | Already hold the resolved DAIV user from the webhook; source of the `acting_user_id` hop. |

---

## Entity → requirement coverage

| Requirement | Where it lands |
|---|---|
| FR-002 | `PlatformCredential` + the credential service |
| FR-005, FR-006 | Enforced by the platform via the token; refusal vocabulary in [contracts/agent-tools.md](./contracts/agent-tools.md) |
| FR-007–FR-010 | `PlatformCredential.state`, `scopes`, `expires_at`; consent flow |
| FR-012 | Encrypted at rest; cache-only at runtime; absent from state and checkpoints |
| FR-013 | Cache key is the identity, never the thread |
| FR-014 | `(provider, platform_uid)` index + the `acting_user_id` hop |
| FR-015 | Marker on cross-project published content (D9) — content convention, not a model |
| FR-016, SC-007 | `CrossProjectAccessRecord` |
| FR-019 | `SiteConfiguration.cross_project_access_enabled` |
