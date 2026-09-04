# Cross-Project Access

A DAIV run works on one repository. Sometimes the answer lives in another one — the API contract in a sibling service, the failing pipeline of an upstream library, the issue that explains why a change was made. Cross-project access lets the agent reach those projects **as the person who asked**, so the git platform's own permission check decides what comes back.

It is **off by default**. Enabling it changes what the agent can reach, so an upgrade never turns it on for you.

---

## What changes when it is on

The `gitlab` and `gh` agent tools gain one optional argument, `project`:

| `project` | Target | Identity |
|---|---|---|
| empty (the default) | the repository the run is attached to | DAIV's own service identity — exactly as before |
| the attached repository's own path | same as above | same as above |
| any other project path | that project | the OAuth credential of the person who requested the run |

Everything else is unchanged: the same 30-second timeout, the same automatic saving of oversized
results, the same blocked GitHub `api` resource. Two things are narrower outside the attached
project — see [What a cross-project call may not do](#what-a-cross-project-call-may-not-do).

!!! warning "The attached project always uses DAIV's identity"
    Comments, merge requests and commits on the repository a run is attached to are still authored by DAIV, whether or not this feature is on. Only calls that name a *different* project act as a person.

### Refusals, not silence

A project the requesting person cannot reach is **refused with a stated reason** — never returned as an empty result, and never retried under DAIV's own identity. Each cause gets its own message, so the reader learns which thing is wrong:

| Cause | What the person is told |
|---|---|
| Capability off | Only the attached project can be reached on this deployment |
| No requesting user | The run has no signed-in person, so only the attached project can be reached |
| Not authorised | They have not authorised DAIV; the message points at their account settings |
| Expired / revoked | The authorisation is gone and must be granted again |
| Insufficient scope | The authorisation is narrower than the operation needs |
| Platform denied | The project is not accessible to them — deliberately ambiguous between "does not exist" and "you may not see it", so the tool cannot be used to probe for private projects |

---

## Turning it on

1. Configure the OAuth application for your platform (below) and re-authorise.
2. In **Site Configuration → Agent tools → Cross-project access**, switch **cross-project access enabled** on.

Nothing needs a redeploy.

---

## OAuth application setup

DAIV uses the **same OAuth application** that already powers dashboard sign-in — the one configured under **Site Configuration → Authentication** (`auth_client_id` / `auth_client_secret`). There is no second credential pair. What changes is the *scope* it requests.

### GitLab

Requested scopes become `read_user api`, up from `read_user` alone. This happens on every
sign-in, whether or not cross-project access is switched on, so the sign-in page always discloses
the wider authorisation it is about to request.

| Scope | What it buys |
|---|---|
| `read_user` | identity, as today |
| `api` | read **and write** in other projects, bounded by that person's own permissions |
| `read_api` | read-only alternative — cross-project writes then fail at the platform |

Override the requested set with the `DAIV_GITLAB_OAUTH_SCOPE` environment variable (space-separated). A deployment that only wants read-only cross-project context sets:

```bash
DAIV_GITLAB_OAUTH_SCOPE="read_user read_api"
```

Update the redirect URI list on your GitLab application if it is not already correct for dashboard login — it is unchanged by this feature.

!!! note "Short-lived tokens, with rotation"
    GitLab access tokens expire (2 hours by default) and GitLab **rotates the refresh token on every use**. DAIV renews at the point of use, within a five-minute margin of expiry, and writes the new access token, refresh token and expiry in one transaction. Only GitLab refusing the grant itself (`invalid_grant`) marks the authorisation expired; a timeout, a 5xx or an unreadable answer leaves it in place and fails just that call, so a momentary outage does not cost the person a re-authorisation.

### GitHub

DAIV uses the **GitHub App's user-to-server flow**, reusing the App you already configured (`CODEBASE_GITHUB_APP_ID`, `CODEBASE_GITHUB_PRIVATE_KEY`). No OAuth App and no second set of credentials.

GitHub Apps **ignore the OAuth `scope` parameter** entirely. A user-to-server token's reach is the *intersection* of what the person can do and what the App is installed on — materially tighter than a classic OAuth `repo` scope, which would grant every repository the person can touch. What the operator controls is the App's own permissions:

| Permission | Access |
|---|---|
| Issues | Read (Read & write for cross-project comments) |
| Pull requests | Read (Read & write for cross-project comments) |
| Actions | Read |
| Metadata | Read |

Both "Expire user authorization tokens" settings work. Enabled gives 8-hour tokens plus refresh tokens; disabled gives non-expiring tokens and renewal is a no-op. Renewal posts to the host `CODEBASE_GITHUB_URL` names, so a GitHub Enterprise deployment renews against its own server rather than github.com.

!!! warning "The App must be installed on the target"
    A user-to-server token cannot reach a repository the App is not installed on, even when the person can. Install the App on every organisation whose repositories the agent should be able to read.

---

## Every existing user must re-authorise

This is a visible behaviour change for people who already use DAIV.

- **Before this release**, DAIV requested identity-only scopes and stored no user token at all. Nobody has an authorisation wide enough for cross-project access.
- **After**, signing in again grants the wider authorisation and stores it, encrypted.
- **Until they do**, everything they could do before still works. Only a cross-project call is refused, and the refusal names re-authorisation as the next step.

Tell people they may re-authorise from **Account → Git authorisation** in the dashboard, where they can also see the state of their authorisation, when its current token expires, which scopes the platform actually granted, and a **Disconnect** action that clears the stored secrets immediately.

!!! note "Disconnect is local"
    Disconnecting clears what DAIV stored; it does not withdraw the authorisation on the git
    platform. Because the platform still holds the grant, signing in to DAIV again completes
    without a fresh consent prompt and restores it. To withdraw it for good, remove DAIV from the
    authorised applications in the git platform's own settings.

---

## Where the credential lives

| | |
|---|---|
| Stored | One row per person per platform host, in DAIV's own database |
| Encryption | Fernet, using `DAIV_ENCRYPTION_KEY` — the same key that protects every other stored secret |
| In memory | Cached briefly in Redis, keyed on the **identity**, never on the conversation |
| Never | In agent state, in a LangGraph checkpoint, in a log line, in a diff, or in published content |

DAIV does **not** use allauth's own `SocialToken` table: it stores tokens in plaintext, has no revocation state, and is recreated on every login.

!!! danger "Rotating `DAIV_ENCRYPTION_KEY` invalidates every stored authorisation"
    The stored access and refresh tokens are encrypted with that key. Rotate it and no credential can be decrypted any more: the next cross-project call clears the unreadable grant and asks that person to re-authorise, so **every user must sign in again**, on top of re-entering the configuration secrets the key already protects (see [Site Configuration](../reference/site-configuration.md)). There is no migration path that preserves them; a stored token is not worth a key-rotation escape hatch.

---

## Audit trail

Every attempt to reach another project writes one record — allowed *and* refused — visible to admins at **Cross-project access** in the sidebar, filterable by target project, thread and outcome.

A record holds who acted, which project they reached, under which identity, on which thread, and how it ended. It holds **no token and no content fetched from the target project**: it proves *that* a project was reached, never *what* was in it. Rows are pruned automatically after 90 days (`CODEBASE_CROSS_PROJECT_RECORD_RETENTION_DAYS`).

---

## Self-triggered runs

A comment DAIV leaves in another project carries that person's attribution, not the bot's — so the usual "is this my own event?" check cannot recognise it, and a DAIV-watched target project would feed DAIV's own comment straight back as a new run. DAIV appends a non-rendering marker to the body of anything it writes cross-project, and webhook handling ignores comments carrying it. A comment the *person* writes there themselves is handled normally.

---

## Platform differences

Parity is functional: both platforms reach the same capability. Two mechanical differences remain, and neither can be closed from DAIV's side.

| | GitLab | GitHub |
|---|---|---|
| What bounds reach | The scopes the person consented to | The App's installed permissions ∩ the person's own |
| Narrowing to read-only | `DAIV_GITLAB_OAUTH_SCOPE="read_user read_api"` | Not available as a per-deployment switch — set the App's permissions to Read |
| Token lifetime | Always expiring, always rotating | Expiring only if the App enables it |
| Reaching a repo DAIV is not installed on | Possible | **Not possible** — the App must be installed |

### What a cross-project call may not do

Two limits apply outside the attached project and nowhere else.

**Inline merge request diff comments can only be created on the attached project.** The inline path
goes through DAIV's own platform client, which holds the service token — the one identity a
cross-project call may not use. A regular note works cross-project.

**Destructive verbs are refused by policy, not by the platform.** The person's own token would
carry them, so the platform will not object; but what the token is spent on can be chosen by issue
or comment text somebody else wrote. Reads, and the issue, merge-request and note writes the agent
exists to make, still cross. These do not:

| GitLab | GitHub |
|---|---|
| `project delete-merged-branches`, `project trigger-pipeline` | `run rerun`, `workflow run` |
| `project-pipeline create/cancel/retry`, `project-merge-request-pipeline create` | `issue close/reopen/lock/unlock/develop` |
| `project-job retry/play` | `pr close/reopen/lock/unlock` |
| `project-branch create`, `project-tag create` | `release create/edit/upload` |
| `project-release create/update`, `project-release-link create/update` | `cache delete` |
| any award-emoji `delete`, `project-issue-link delete`, `project-merge-request-draft-note delete` | |

Each refusal is recorded in the access log with the outcome **Denied — not permitted
cross-project**. All of them remain available on the attached project, under DAIV's own identity.

---

## Related pages

- [Accounts & Roles](../getting-started/accounts.md) — sign-in and OAuth configuration
- [Site Configuration](../reference/site-configuration.md) — the toggle and the Authentication section
- [Environment Variables](../reference/env-variables.md) — `DAIV_GITLAB_OAUTH_SCOPE`, `DAIV_ENCRYPTION_KEY`
