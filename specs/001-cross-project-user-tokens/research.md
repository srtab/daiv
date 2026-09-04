# Phase 0 Research: Cross-Project Git Platform Access Under the Requesting User's Identity

**Date**: 2026-09-03 | **Spec**: [spec.md](./spec.md)

All Technical Context unknowns are resolved below. Findings are grounded in the current code, not
assumed; file references are given so the planning decisions can be re-checked.

## Current state (what the code actually does)

Both agent tools live in one middleware, [git_platform.py](daiv/automation/agent/middlewares/git_platform.py),
and shell out to a CLI with a hard-pinned project:

| | GitLab | GitHub |
|---|---|---|
| Credential | `GITLAB_PRIVATE_TOKEN` env ← `settings.GITLAB_AUTH_TOKEN`, a single deployment-wide service token ([git_platform.py:643](daiv/automation/agent/middlewares/git_platform.py:643)) | `GH_TOKEN` env ← GitHub **App installation** token, minted per thread and cached ([git_platform.py:709](daiv/automation/agent/middlewares/git_platform.py:709)) |
| Project pinning | `--project-id <slug>` appended unconditionally ([git_platform.py:661](daiv/automation/agent/middlewares/git_platform.py:661)) | `--repo <slug>` appended unconditionally ([git_platform.py:818](daiv/automation/agent/middlewares/git_platform.py:818)) |
| Escape hatches closed | `--project-id=` rejected ([:515](daiv/automation/agent/middlewares/git_platform.py:515)) | `--repo`/`-R`/`--hostname` rejected ([:744](daiv/automation/agent/middlewares/git_platform.py:744)); the `api` resource is excluded from the allowlist *specifically* because it would bypass `--repo` |

`settings.CLIENT` selects **one** platform per deployment ([conf.py:10](daiv/codebase/conf.py:10)), so a
single run never spans GitLab *and* GitHub. Spec's cross-host edge case reduces to "same host, other project".

Identity plumbing that already exists and is reusable:

- `RuntimeCtx.acting_user_id` ([context.py:92](daiv/codebase/context.py:92)) — set for chat and MCP job runs.
- `resolve_user(provider, uid, username, email)` ([accounts/utils.py:16](daiv/accounts/utils.py:16)) — platform user → DAIV user. **Already called by every webhook callback** ([gitlab/api/callbacks.py:133](daiv/codebase/clients/gitlab/api/callbacks.py:133)).
- `aget_platform_identity(user_id, provider)` ([accounts/utils.py:62](daiv/accounts/utils.py:62)) — the reverse.
- Fernet field descriptors over `core/encryption.py`, used today for `Provider` secrets ([core/models.py:95](daiv/core/models.py:95)).

---

## D1 — How a call selects another project

**Decision**: add an explicit, optional `project` argument to both tools. Empty ⇒ the attached
repository (today's behaviour, FR-004). Non-empty and different ⇒ the cross-project path (FR-002).
The value is injected into the existing `--project-id` / `--repo` flag position; the tool keeps
rejecting those flags inside `subcommand`.

**Rationale**: the project stays a structured, validated argument the middleware controls. The
`shlex`-split `subcommand` string never becomes the place where the security-relevant target is
decided, so the existing "these flags are managed" guards keep their meaning instead of becoming
conditional.

**Alternatives rejected**:
- *Allow `--project-id` / `--repo` inside `subcommand`*. Makes target selection depend on parsing an
  agent-authored string; the flag guards would have to become an allowlist, and `_parse_gitlab_flag`
  would become security-load-bearing. Strictly more attack surface for no gain.
- *A separate `gitlab_other_project` tool*. Doubles the tool surface and the description budget, and
  the model would have to choose between near-identical tools.

**Validation required**: reject a `project` value that is empty after strip, starts with `-` (it would
be read as a flag in `argv`), or contains whitespace/control characters. `argv` is passed to
`create_subprocess_exec` without a shell, so quoting is not the risk — flag confusion is.

**GitHub `api` stays blocked.** Its exclusion reason ([git_platform.py:813](daiv/automation/agent/middlewares/git_platform.py:813))
is unchanged by this feature: it would bypass whichever repo flag we set.

## D2 — Where the per-person credential lives

**Decision**: a DAIV-owned model holding the access token, refresh token, and expiry, encrypted at
rest with the existing Fernet descriptor pattern. Populated from allauth's social-login signal.

**Rationale**: allauth's own `SocialToken` stores tokens **in plaintext**, and
`SOCIALACCOUNT_STORE_TOKENS` defaults to `False` — verified in
`allauth/socialaccount/app_settings.py:147` — so today DAIV keeps no user tokens at all. A DAIV-owned
row also carries the state the spec needs (`connected` / `expired` / `revoked`, FR-008) which
`SocialToken` has no field for, and lets revocation delete the secret without unlinking the account.

**Alternatives rejected**:
- *Set `SOCIALACCOUNT_STORE_TOKENS=True` and read `SocialToken`*. One line, but plaintext secrets at
  rest, no revocation state, and the row is destroyed/recreated by allauth on re-login.
- *Ask the user to paste a Personal Access Token*. Already ruled out during `/speckit-clarify`.

## D3 — GitHub: OAuth App vs GitHub App user-to-server

**Decision**: GitHub App **user-to-server** tokens, reusing the existing App's client credentials.

**Rationale**: a user-to-server token's reach is the *intersection* of what the person can do and
what the App is installed on. A classic OAuth App `repo` scope grants every repository the person can
touch, including ones DAIV was never installed on. The intersection is materially tighter and is the
best available answer to Principle II's least-privilege requirement, which a per-person credential
otherwise strains. It also avoids provisioning a second set of GitHub credentials alongside the App
DAIV already requires (`GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY`, `GITHUB_INSTALLATION_ID`).

**Cost, stated plainly**: GitHub Apps ignore the OAuth `scope` parameter — reach is set by the App's
configured permissions, so the operator must grant the App read on issues/PRs/actions. allauth's stock
`github` provider needs an adapter subclass for the App's authorize/token endpoints and for expiring
tokens, mirroring what `GitLabServerAwareAdapter` ([accounts/socialaccount.py](daiv/accounts/socialaccount.py))
already does for GitLab.

**Alternatives rejected**: *Classic OAuth App with `repo` scope* — simpler and stock-supported by
allauth, but grants a strictly wider token than the App installation, and needs separate credentials.

**Open operator choice, not a blocker**: whether the App has "Expire user authorization tokens"
enabled. Enabled ⇒ 8-hour tokens plus refresh tokens (D5 applies). Disabled ⇒ non-expiring tokens
(refresh is a no-op). Both must work; the code branches on whether an expiry was returned.

## D4 — GitLab OAuth scope

**Decision**: request `api`, with the requested scope operator-configurable and `read_api` as the
documented narrower option.

**Rationale**: the spec's assumptions permit cross-project writes, governed by the person's own
permissions rather than a DAIV-side read-only rule; `read_api` cannot satisfy that. Making it
configurable lets an operator who only wants read context choose the narrower grant without a code
change — the same operator-decides posture Principle I takes on egress.

Today's scopes are `{"github": ["user:email"], "gitlab": ["read_user"]}`
([allauth.py:33](daiv/daiv/settings/components/allauth.py:33)) — identity only, which is why every
existing user must re-consent (FR-010).

## D5 — Token refresh

**Decision**: refresh on read when the stored token is within a 5-minute margin of expiry; on refresh
failure mark the row `expired` and surface FR-011's message. Never block the attached-project path on it.

**Rationale**: GitLab OAuth access tokens are short-lived (2 hours by default) **with refresh-token
rotation** — the refresh token changes on every use, so a lost write means a dead credential. Refresh
must therefore persist the new refresh token in the same transaction as the new access token. GitHub
App user-to-server tokens behave the same way when expiry is enabled.

This is the same class of bug as the memory note on [egress token intra-turn expiry] — a token minted
at turn start expiring before a later call in the same turn. Refresh-on-read at the point of use, not
once per run, is what avoids it.

## D6 — Caching, and why the user token must not reach agent state

**Decision**: user tokens go to the Redis cache only, keyed by `(user_id, provider, host)`, never into
`GitPlatformState` and never into a checkpoint. The existing service-token state field is untouched.

**Rationale**: this is the sharpest constitutional conflict in the feature. The GitHub tool currently
writes its token into agent state ([GitPlatformState](daiv/automation/agent/middlewares/git_platform.py:872))
via a `Command(update=...)`, and agent state is checkpointed. Principle II forbids persisting tokens
in checkpoints, and FR-012 restates it. That is tolerable today because the value is a short-lived
*installation* token belonging to DAIV; it is not tolerable for a credential belonging to a person.

The existing cache key is the **`thread_id`** ([git_platform.py:726](daiv/automation/agent/middlewares/git_platform.py:726)).
Re-using that shape for user tokens would break FR-013 the moment two identities touch one thread —
a resumed session whose acting person differs from the original would read the previous person's
token. The key must be the identity, never the conversation.

## D7 — Resolving the acting person on webhook runs (FR-014)

**Decision**: plumb `acting_user_id` from the already-resolved `Session`/run row through the
webhook task chain into `set_runtime_ctx`.

**Rationale**: the mapping already exists and already runs. Every webhook callback calls
`resolve_user(...)` and passes the result to `acreate_run(user=daiv_user, ...)`
([callbacks.py:133](daiv/codebase/clients/gitlab/api/callbacks.py:133)), but `address_issue_task`
never forwards it to `set_runtime_ctx`, so `ctx.acting_user_id` is `None` for these runs — which is
exactly what the `RuntimeCtx` docstring says today. FR-014 needs no new resolution logic, only the
missing hop. GitHub's callbacks follow the same shape.

**Note for `/speckit-tasks`**: `resolve_user` matches on username *then* email *then* social uid. The
username and email branches match a DAIV account that may never have linked that platform identity —
fine for choosing an MR assignee (its current use), but for selecting whose *credential* to act with,
only a row that proves the link is acceptable. The credential lookup must key on the stored
credential's own platform uid, not merely on the resolved DAIV user.

## D8 — Why `RepositoryAccess` must **not** gate cross-project calls

**Decision**: the person's token is the sole enforcement point. The existing `RepositoryAccess`
mirror is not consulted to permit or deny a cross-project call.

**Rationale**: the mirror is populated by one member-list call **per bot-visible repository**
([tasks.py:53](daiv/codebase/tasks.py:53), [models.py:103](daiv/codebase/models.py:103)). Its universe
is what the *bot* can see. A person may well have access to a project DAIV was never installed on —
which is precisely the interesting cross-project case — and the mirror would deny it. Gating on it
would make the feature fail exactly where it is most useful, while adding a second permission model
the spec explicitly rules out.

The mirror stays what it is: authorization for DAIV's own dashboard and repo picker
([authorization.py](daiv/codebase/authorization.py)). Unchanged by this feature.

## D9 — Recognising DAIV's own cross-project content (FR-015)

**Decision**: append a stable, non-rendering marker to any content published in a project other than
the attached one, and check for it in the webhook `accept_callback` path.

**Rationale**: the attached project needs nothing — it keeps the service identity, so the existing
`self.user.id == self._client.current_user.id` check ([callbacks.py:176](daiv/codebase/clients/gitlab/api/callbacks.py:176))
still recognises DAIV's own events. Only cross-project writes carry a person's attribution, and only
a project that is *itself* DAIV-watched can feed an event back. The residual risk is narrow but real,
and FR-015 is a MUST, so the marker is cheap insurance rather than speculative work.

**Alternative rejected**: *make cross-project calls read-only*. It would dissolve FR-015 entirely and
is genuinely simpler — but the spec's assumptions place that decision with the person's platform
permissions, not with DAIV. Recorded here so it can be reopened deliberately rather than by drift.

## D10 — Operator switch (FR-019)

**Decision**: a `SiteConfiguration` boolean, following `web_search_enabled` / `auth_login_enabled`
([core/models.py:382](daiv/core/models.py:382), [:433](daiv/core/models.py:433)). Off ⇒ the `project`
argument is not offered to the model and the cross-project branch is unreachable.

**Rationale**: matches the existing in-repo pattern for optional agent capabilities; runtime-toggleable
without a redeploy.

## Resolved unknowns

| Unknown | Resolution |
|---|---|
| Does allauth store OAuth tokens today? | No — `SOCIALACCOUNT_STORE_TOKENS` defaults to `False` and is unset. Nothing to migrate. |
| Can one run span both platforms? | No — `settings.CLIENT` is deployment-wide. |
| Is the platform-user → DAIV-user mapping available on webhooks? | Yes, already resolved and stored; only the task-chain hop is missing. |
| Encryption-at-rest precedent? | Yes — `core/encryption.py` + descriptors in `core/models.py`. No new dependency. |
| New dependencies? | None. `cryptography`, `django-allauth`, `python-gitlab`, `PyGithub` are all present and pinned. |
