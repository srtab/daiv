# Cross-Project Access

A DAIV run works on one repository. Sometimes the answer lives in another one: the API contract in a sibling service, the failing pipeline of an upstream library, the issue that explains why a change was made. Cross-project access lets the agent reach those projects **as the person the run acts for**, so the git platform's own permission check decides what comes back. It can read them, comment, file issues and edit the fields of existing issues and merge requests. Opening merge or pull requests, closing, locking, approving, driving CI and similar operations are refused. A conversation that fetched from another project then belongs to that person: only they and admins see it, and only they continue it, see [Conversations that reached another project](#conversations-that-reached-another-project).

It is **off by default**, and upgrading never turns it on.

---

## What changes when it is on

The `gitlab` and `gh` agent tools gain one optional argument, `project`. With the switch off, the tools have no `project` argument at all.

| `project` | Target | Identity |
|---|---|---|
| empty (the default) | the repository the run is attached to | DAIV's own service identity, exactly as before |
| the attached repository's own path | same as above | same as above |
| any other project path | that project | the OAuth credential of the person the run acts for |

Everything else is unchanged: the same 30-second timeout, the same automatic saving of oversized results, the same blocked GitHub `api` resource. Outside the attached project the agent can do less, see [What the agent can and cannot do in another project](#what-the-agent-can-and-cannot-do-in-another-project).

!!! warning "The attached project always uses DAIV's identity"
    Comments, merge requests and commits on the repository a run is attached to are authored by DAIV, whether or not this feature is on. Only calls that name a *different* project act as a person.

The `project` value must be a project path on the platform DAIV is configured for: `group/name` on GitLab, `owner/name` on GitHub. A flag, a URL on another host, or anything that is not a single path is refused.

!!! note "One `gh` check applies with the switch off too"
    `gh` takes the repository from an issue or pull request URL rather than from `--repo`, so `gh issue view https://github.com/acme/other/issues/1` would reach `acme/other` under DAIV's own GitHub App token. On every call, whether or not this feature is on, `gh` refuses an issue or pull request URL, or an `owner/repo#N` reference, that names a repository other than the call's target. The agent is told to pass the number instead and, with the switch on, to set `project`. A URL that names the target itself on the configured host, compared without regard to case, still works. GitLab is unaffected: python-gitlab takes every value as a `--flag`, and none of its commands accepts a positional argument.

### Refusals, not silence

A project the person cannot reach is **refused with a stated reason**. It is never returned as an empty result, and never retried under DAIV's own identity. Each cause gets its own message, so the reader learns which thing is wrong:

| Cause | What the agent is told |
|---|---|
| Switched off | Cross-project access is not enabled on this deployment; only the attached project can be reached. A run that started while the switch was on gets this if you switch it off mid-run |
| Webhook runs not allowed | Cross-project access is not enabled for runs started by an issue or merge request event (see [Webhook-triggered runs](#webhook-triggered-runs)) |
| No requesting person | The run has no signed-in person and was not started by a platform account, so only the attached project can be reached |
| Not authorised | The person has not authorised DAIV; the message points at their account settings |
| Expired or revoked | The authorisation is gone and must be granted again |
| Insufficient scope | The authorisation is narrower than the operation needs |
| Rejected by the platform | The platform refused the token for this call. DAIV drops its cached copy and tries again on the next call; if it keeps failing, the person re-authorises |
| Renewal failed | The platform did not answer the renewal. The authorisation is intact; retry shortly |
| Unreadable | The stored authorisation can no longer be decrypted (see [Where the credential lives](#where-the-credential-lives)) and was cleared |
| Not accessible | The project is not accessible to the person. This is deliberately ambiguous between "does not exist" and "you may not see it", so the tool cannot be used to probe for private projects |
| Not permitted cross-project | The person holds the permission, but DAIV refuses the operation outside the attached project (see [below](#what-the-agent-can-and-cannot-do-in-another-project)) |
| Result withheld | The call ran, but DAIV could not write its audit record, which is what keeps the result to the person it was fetched for. The result is not returned; a write may already have happened |

The agent never sees the platform's own error text from another project. It can carry token fragments, and repository names the person is not entitled to see.

---

## Turning it on

1. Prepare the OAuth application: on GitLab, allow the scope the feature requests; on GitHub, sign in with the GitHub App's own credentials. See [OAuth application setup](#oauth-application-setup).
2. In **Site Configuration → Agent tools → Cross-project access** (`/dashboard/configuration/cross_project/`), switch **cross-project access enabled** on. The same field can be set with `DAIV_CROSS_PROJECT_ACCESS_ENABLED`.
3. Tell people to sign in again so DAIV can store their authorisation (see [Upgrading](#upgrading)).

Nothing needs a redeploy. The switch is read when a run starts: a run already in progress when you switch it on does not get the `project` argument, and one already in progress when you switch it off has every cross-project call refused.

---

## OAuth application setup

DAIV uses the **same OAuth application** that powers dashboard sign-in, the one configured under **Site Configuration → Authentication** (`auth_client_id` / `auth_client_secret`). There is no second credential pair. What changes is the *scope* it requests, and only while cross-project access is on.

### GitLab

While the switch is on, sign-in requests `read_user api` instead of `read_user` alone.

| Scope | What it buys |
|---|---|
| `read_user` | identity, as before |
| `api` | read **and write** in other projects, bounded by that person's own permissions |
| `read_api` | read-only alternative: cross-project writes then fail at GitLab and reach the agent as a refusal |

`DAIV_GITLAB_OAUTH_SCOPE` sets the requested scopes (space-separated). Its default is `read_user api`. A deployment that only wants read-only cross-project context sets:

```bash
DAIV_GITLAB_OAUTH_SCOPE="read_user read_api"
```

!!! warning "The GitLab Application must allow the scope first"
    GitLab refuses any authorisation that asks for more than the Application grants. Tick `api` (or `read_api`) on the existing Application under **Admin Area → Applications**, or set `DAIV_GITLAB_OAUTH_SCOPE` to what it already grants, **before** you turn the switch on. Otherwise the next GitLab sign-in fails with *"The requested scope is invalid, unknown, or malformed."* for everyone. Email login-by-code is unaffected.

!!! warning "`ALLAUTH_GITLAB_URL` and `CODEBASE_GITLAB_URL` must name the same GitLab"
    The token is minted against the sign-in GitLab (`ALLAUTH_GITLAB_SERVER_URL` when set, otherwise `ALLAUTH_GITLAB_URL`) and spent against `CODEBASE_GITLAB_URL`. If the hosts differ, DAIV logs an error and does not store the credential, rather than send one instance's token to another.

!!! note "Short-lived tokens, with rotation"
    GitLab access tokens expire (2 hours by default) and GitLab **rotates the refresh token on every use**. DAIV renews at the point of use, within five minutes of expiry, and writes the new access token, refresh token and expiry in one transaction. Only GitLab refusing the grant itself (`invalid_grant`) marks the authorisation expired. A timeout, a 5xx or an unreadable answer leaves it in place and fails just that call, so a momentary outage does not cost the person a re-authorisation.

### GitHub

DAIV uses the **GitHub App's user-to-server flow**, on the App you already run (`CODEBASE_GITHUB_APP_ID`, `CODEBASE_GITHUB_PRIVATE_KEY`). For the tokens to be user-to-server, sign-in must use that **App's** credentials, not a separate OAuth App's:

| Setting | Value |
|---|---|
| OAuth client ID (`ALLAUTH_CLIENT_ID`) | the App's **Client ID** (not its App ID) |
| OAuth client secret (`ALLAUTH_CLIENT_SECRET`) | a client secret generated on the App |
| App **Callback URL** | `https://<your-domain>/accounts/github/login/callback/` |
| App account permission **Email addresses** | Read-only, so sign-in can read the person's verified email |

A plain OAuth App still signs people in, but its tokens carry only the `user:email` scope DAIV requests, so GitHub refuses them on private repositories.

GitHub Apps **ignore the OAuth `scope` parameter** entirely. A user-to-server token's reach is the *intersection* of what the person can do and what the App is installed on, which is materially tighter than a classic OAuth `repo` scope that would grant every repository the person can touch. What you control is the App's own permissions:

| Permission | Access |
|---|---|
| Issues | Read (Read & write for cross-project issues and comments) |
| Pull requests | Read (Read & write for cross-project pull requests and comments) |
| Actions | Read |
| Metadata | Read |

Both "Expire user authorization tokens" settings work. Enabled gives 8-hour tokens plus refresh tokens; disabled gives non-expiring tokens and renewal is a no-op. Renewal posts to the host `CODEBASE_GITHUB_URL` names, so a GitHub Enterprise deployment renews against its own server rather than github.com.

On GitHub Enterprise Server the calls go to that host too. `gh` reads `--repo owner/name` as github.com unless told otherwise, so each cross-project call sets `GH_HOST` to the `CODEBASE_GITHUB_URL` host and passes the person's token as `GH_ENTERPRISE_TOKEN`, the variable `gh` reads for any host other than github.com. On github.com it stays `GH_TOKEN`.

---

## Upgrading

- **Upgrading with the switch off changes nothing.** Sign-in still requests identity-only scopes, DAIV stores no user token, and nobody has to sign in again.
- **Turning the switch on is the change.** From then on sign-in requests the wider authorisation, says so on the sign-in page, and stores the result encrypted. People who signed in earlier have nothing stored yet. They re-authorise by signing in again, or from **Account → Git authorisation**.
- **Until they do**, everything they could do before still works. Only a cross-project call is refused, and the refusal names re-authorisation as the next step.
- **Before you turn it on**, make sure the GitLab Application allows the scope, as described [above](#gitlab).
- **Turning it off again** stops every use of stored authorisations at once. It does not delete them: people can still **Disconnect**, and a stored authorisation is used again if you switch the feature back on.

---

## Who a run acts for

A cross-project call spends the authorisation of exactly one person, chosen by how the run started:

| How the run started | Acts for |
|---|---|
| Chat, the Jobs API, MCP, a dashboard job, a scheduled job | The DAIV user who started it (a schedule's owner, for scheduled jobs) |
| An issue labelled `daiv`, `daiv-max` or `daiv-auto`, or an `@daiv` mention on an issue or merge request | The git platform account that added the label or wrote the mention, and only if that exact account signed in to DAIV and authorised it. Needs [webhook-triggered runs](#webhook-triggered-runs) to be on |
| A CI-fix run on a session that a signed-in person started | The same person as the session |
| A CI-fix run on a session that a webhook started | No one. Cross-project calls are refused |

A webhook run is matched by the platform's own account id, never by a username or an email address, so a person cannot borrow another's authorisation by sharing a name. If two DAIV accounts hold an authorisation for the same platform account, DAIV refuses both and logs an error. A deactivated user's authorisation is never used.

---

## Conversations that reached another project

What a cross-project call returns lands in the conversation's transcript, alongside everything else the agent saw. So the first call in a session that is **allowed** to fetch from another project marks the session with the DAIV user whose authorisation it spent. From then on:

| | Who |
|---|---|
| Sees the session | Only the people on its mark, and admins. That covers the session page and transcript, its runs (including the Markdown download), its artifacts, the live chat stream, the Jobs API and MCP `get_job_status` / `list_jobs`, the sessions and artifacts lists and the dashboard counts. Everyone else loses it, including people who acted in it, people who can read the attached repository, and subscribers of the schedule that started it |
| Is notified about its runs | The same people. A schedule's subscribers stop getting its runs' summaries; a batch rollup leaves out anyone who may not see every run in it |
| Continues it | Only a person on its mark, signed in to DAIV (chat, the Jobs API, MCP, a dashboard job, a schedule). Any other run is refused before the agent starts, with the message below |
| Mines it into repository memory | Nobody. Runs in a marked session are never extracted into memory |

The refusal reads:

> This conversation holds results fetched from other projects on another person's behalf, so DAIV won't continue it here. Start a new conversation or issue.

Chat shows it as an error, an issue or merge request gets it as a reply to the mention, and a job ends **FAILED** with it as its `error`, still readable by whoever submitted it. It holds nothing from the other project.

The mark covers runs that cannot prove a DAIV sign-in, so it also refuses:

- **every webhook run** in the session, even one the same person triggers with a mention, because a webhook run carries a platform account, not a DAIV sign-in. An issue thread where a cross-project fetch happened stops answering `@daiv`; open a new issue to continue the work there;
- **CI-fix runs** on a session that a webhook started;
- **a different signed-in person**, admins included.

People outside the mark cannot even open the session, so for them the Jobs API and MCP answer a `thread_id` continuation as an unknown thread. The mark is never removed. A fetch DAIV cannot attribute to a person should not happen; if it does, DAIV logs an error and leaves the session to admins alone.

---

## Webhook-triggered runs

Runs that start from an issue label or a mention are covered by a second switch, **allow for webhook-triggered runs** (`cross_project_webhook_runs_enabled`, or `DAIV_CROSS_PROJECT_WEBHOOK_RUNS_ENABLED`), in the same **Cross-project access** group. It is off by default and has no effect while the main switch is off. With it off, those runs are refused with a message saying so, and reach only the attached project. With it on, a webhook run that fetches from another project [marks its session](#conversations-that-reached-another-project), after which further mentions on that issue or merge request are refused.

!!! warning "Issue text can steer these runs"
    Anyone who can open or comment on an issue writes text the agent reads. With this switch on, that text can influence which other projects the agent reads and what it posts or edits there, using the permissions of the person whose label or mention started the run. Turn it on only where you trust everyone who can write to the projects DAIV watches.

---

## What the agent can and cannot do in another project

Outside the attached project the agent can read through every subcommand the tools allow, and it can write in the ways listed under [What still crosses](#what-still-crosses). The agent's own tool description lists the same rules. Everything else is refused by DAIV's policy even where the person's own permissions would allow it, because what the token is spent on can be chosen by issue or comment text somebody else wrote. The refused operations stay available on the attached project, under DAIV's own identity.

Every refusal is recorded in the [audit log](#audit-log) as **Denied — not permitted cross-project**, and reaches the agent with the reason, before any token is spent.

### What still crosses

| | GitLab | GitHub |
|---|---|---|
| Creates | issues (a confidential one included), notes, discussions and discussion notes (issues, merge requests, snippets), issue links, award emoji, merge request draft notes | issues, comments |
| Edits an issue or merge/pull request with `update` or `edit` | title, description, labels, milestone (`--milestone-id`) and, on merge requests, reviewers (`--reviewer-ids`); on issues also `--due-date`; on merge requests also `--squash`, `--remove-source-branch` and `--allow-maintainer-to-push` | title, body, labels (`--add-label`, `--remove-label`) and projects (`--add-project`, `--remove-project`); on pull requests also reviewers (`--add-reviewer`, `--remove-reviewer`) |
| Sets people when creating | none | `--assignee` on `issue create` |
| Other writes | `time-estimate` and `add-spent-time` on issues and merge requests, `project-issue reorder`, `project-merge-request-draft-note update` | none |

!!! warning "Edits can rewrite what somebody else wrote"
    Nothing refuses an edit to the title or description of an issue or merge request, including one somebody else wrote. The person's own permissions on the platform are the only check on these edits. Weigh that before you enable [webhook-triggered runs](#webhook-triggered-runs).

### Refused subcommands

| Kind | GitLab | GitHub |
|---|---|---|
| Closes, reopens, locks or approves | none; GitLab closes and locks through flags, see below | `issue close/reopen/lock/unlock`, `pr close/reopen/lock/unlock`, `pr review` (an approval can release auto-merge) |
| Opens a merge or pull request, which starts CI there | `project-merge-request create` | `pr create` |
| Drives CI | `project trigger-pipeline`, `project-pipeline create/cancel/retry`, `project-merge-request-pipeline create`, `project-job retry/play` | `workflow run`, `run rerun`, `cache delete` |
| Creates branches, tags or releases | `project-branch create`, `project-tag create`, `project-release create/update`, `project-release-link create/update` | `issue develop`, `release create/edit/upload` |
| Changes project configuration | `project-label create/update`, `project-snippet create/update` | `label create/edit` |
| Deletes or relocates data | `project delete-merged-branches`, `project-issue move`, and `delete` on award emoji, `project-issue-link` and `project-merge-request-draft-note` | none |
| Edits text somebody else wrote | `update` on notes, discussions and discussion notes of issues, merge requests and snippets | none; see `--edit-last` below |
| Resets time tracking | `reset-spent-time` and `reset-time-estimate` on issues and merge requests | none |

### Refused flags

`update`, `edit` and `comment` stay reachable, so the policy checks them flag by flag. On `create`, `update`, `edit` and `comment`, these flags are refused outright (bot labels, quick actions and file bodies are refused separately, below):

| Platform | Refused flags | What they would do |
|---|---|---|
| GitLab | `--state-event` | close or reopen |
| GitLab | `--discussion-locked` | lock a discussion |
| GitLab | `--target-branch` | repoint a merge request |
| GitLab | `--assignee-id`, `--assignee-ids` | assign |
| GitLab | `--to-project-id` | relocate an issue (only `project-issue move` takes it, and that is refused outright) |
| GitLab | `--confidential`, on `update` only | turn a confidential issue into one everyone who can see the project can read. Creating a confidential issue still crosses |
| GitHub | `--base` | repoint a pull request |
| GitHub | `--milestone`, `--remove-milestone` | change the milestone |
| GitHub | `--add-assignee`, `--remove-assignee` | change assignees |
| GitHub | `--edit-last`, `--delete-last` | rewrite or delete the person's last comment (`issue comment`, `pr comment`). `gh` 2.45 has no `--delete-last`, so it is refused as an unknown flag too |

Every other flag the policy recognises on those commands crosses, as listed above. The two platforms are not symmetric: GitHub refuses milestone changes where GitLab's `--milestone-id` crosses, and GitHub's `--assignee` crosses on `issue create` where GitLab refuses `--assignee-id(s)` on every write.

### Other refusals

| What is refused | Why |
|---|---|
| Adding `daiv`, `daiv-max` or `daiv-auto` as a label (GitLab `--labels`; GitHub `--label`, `--add-label`) on `create`, `update` or `edit` | The label would start a run in that project, if DAIV watches it, as the person who requested this one. The match ignores case, whitespace and quotes. Other labels are fine |
| GitLab quick actions: a body line starting with `/` in `--body`, `--description` or `--note` | GitLab runs `/close`, `/merge`, `/assign`, `/label` and the rest as the person, past every refusal above. A line that merely begins with a slash, such as a file path, is refused too; the agent rewrites it |
| GitLab values read from a file: any argument starting with `@` | python-gitlab replaces `@path` with that file's content. A body that has to start with a mention is written `@@name`, which posts as `@name` |
| GitHub bodies taken from a file or a template: `--body-file` (`-F`), `--template` (`-T`), `--recover` | The body has to be passed as `--body` text, because the [loop marker](#content-daiv-publishes-in-another-project) cannot be appended to a file the person named |
| Inline merge request diff comments (GitLab `project-merge-request-discussion create --position`) | The inline path goes through DAIV's own platform client, which holds the service token, the one identity a cross-project call may not use. A regular merge request note works |
| A flag DAIV cannot identify: on GitLab an unknown or ambiguous option, on any cross-project call; on GitHub, on the writes that cross (`issue create`, `issue edit`, `issue comment`, `pr edit`, `pr comment`), any flag missing from the table DAIV keeps of `gh` 2.45's own `--help` | DAIV does not guess what a value means. `gh` is installed unpinned, so a newer release can add a flag that writes in a way nobody checked; until the table lists it, it is refused. The agent spells each flag as `--help` lists it |
| A GitHub issue or pull request URL, or `owner/repo#N`, that names a repository other than `project` | `gh` would act on the repository the reference names. The same check runs on the attached project, see [above](#what-changes-when-it-is-on) |

---

## Content DAIV publishes in another project

A comment, issue or merge request that DAIV writes in another project carries the person's attribution, not the bot's. The usual "is this my own event?" check cannot recognise it, and a project DAIV also watches would feed the text straight back as a new run.

To prevent that, DAIV appends a non-rendering marker to every body it publishes in another project (the `--body`, `--description` or `--note` of a new or edited issue, merge request, pull request or comment):

```html
<!-- daiv:cross-project -->
```

Webhook handling ignores events that carry it:

- A **comment** containing the marker never starts a run, even if it mentions `@daiv`.
- An **issue** whose description contains the marker is ignored when it is opened or labelled.

A comment the person writes there themselves is handled normally.

!!! note "Labelling an issue DAIV created in another project starts nothing"
    If someone later adds the `daiv` label to an issue DAIV opened in another project, no run starts, because the description still carries the marker. To run DAIV on that work, open a fresh issue in the project and label that one. Or edit the issue's description, delete the `<!-- daiv:cross-project -->` line, and add the label (remove it first if it is already there).

---

## Known limitations

- **No merge or pull requests in another project.** `project-merge-request create` and `gh pr create` are refused, because opening one starts CI in that project. Ask the agent to open an issue or leave a note there instead.
- **`gh` flags newer than 2.45 are refused on cross-project writes** until DAIV's table of `gh` flags lists them.
- **A shared conversation ends at the first cross-project fetch.** Once a session holds another project's results, only the person who fetched them can continue it, and only by a DAIV sign-in. See [Conversations that reached another project](#conversations-that-reached-another-project).
- **Inline diff comments work only on the attached project.** See the table above; a regular note works everywhere.
- **GitHub: the App must be installed on the target.** A user-to-server token cannot reach a repository the App is not installed on, even when the person can. Install the App on every organisation whose repositories the agent should be able to read.

---

## Audit log

Every attempt to reach another project writes one record, allowed *and* refused. An **Allowed** record also [marks its session](#conversations-that-reached-another-project) with the person it names, so if DAIV cannot write it, the call's result is withheld from the agent. Admins find them at **Cross-project access** in the sidebar (`/codebase/cross-project-access/`), newest first, filterable by target project, thread and outcome.

A record holds **who** acted, **which project** they reached, on **which thread**, **how it ended** and **when**. The person's name is snapshotted onto the row, so deleting their account does not erase the answer. An attempt DAIV cannot attribute to an account shows as "The requesting user".

| Outcome | Meaning |
|---|---|
| Allowed | The call ran |
| Denied — no access | The platform refused the person, or rejected the token |
| Denied — no usable credential | The run has no person, or the person has no usable authorisation: missing, expired, revoked or too narrow |
| Denied — capability disabled | A switch is off |
| Denied — not permitted cross-project | DAIV's policy refused the operation |
| Error | A malformed `project`, a timeout, a failed renewal, an unreadable credential, or a command that failed for another reason |

A record holds **no token, no command and nothing fetched from the target project**: it proves *that* a project was reached, never *what* was in it. Work on the attached project uses DAIV's service token and writes no row.

Records are pruned after `CODEBASE_CROSS_PROJECT_RECORD_RETENTION_DAYS` days, **90 by default**. Set it to `0` to keep them forever. The pruning runs with the repository access sync, every 15 minutes by default (`CODEBASE_REPO_ACCESS_SYNC_CRON`).

---

## Each person's authorisation

People manage their own at **Account → Git authorisation** (`/accounts/git-authorisation/`). The page shows the state (Connected, Expired, Revoked or Not authorised), the platform host, when the current token expires, and the scopes the platform actually granted. It offers **Authorise** (**Re-authorise** while connected) and **Disconnect**, which clears the stored secrets immediately.

!!! note "Disconnect is local, and it sticks"
    Disconnecting clears what DAIV stored and stays in effect across later sign-ins. Only pressing **Authorise** on that page again restores it. It does not withdraw the authorisation on the git platform: to do that, remove DAIV from the authorised applications in the platform's own settings.

---

## Where the credential lives

| | |
|---|---|
| Stored | One row per person per platform host, in DAIV's own database |
| Encryption | Fernet, using `DAIV_ENCRYPTION_KEY`, the same key that protects every other stored secret |
| In memory | Cached briefly in Redis, keyed on the **identity**, never on the conversation |
| Never | In agent state, in a LangGraph checkpoint, in a log record, in a tool result or in a Sentry event |

DAIV does **not** use allauth's own `SocialToken` table: it stores tokens in plaintext, has no revocation state, and is recreated on every login.

!!! danger "Rotating `DAIV_ENCRYPTION_KEY` invalidates every stored authorisation"
    The stored access and refresh tokens are encrypted with that key. Rotate it and no credential can be decrypted any more: the next cross-project call clears the unreadable grant and asks that person to re-authorise, so **every user must sign in again**, on top of re-entering the configuration secrets the key already protects (see [Site Configuration](../reference/site-configuration.md)). There is no migration path that preserves them.

---

## Platform differences

Both platforms reach the same capability. The differences that remain are mechanical, and none can be closed from DAIV's side.

| | GitLab | GitHub |
|---|---|---|
| What bounds reach | The scopes the person consented to | The App's installed permissions ∩ the person's own |
| Narrowing to read-only | `DAIV_GITLAB_OAUTH_SCOPE="read_user read_api"` | Not a per-deployment switch; set the App's permissions to Read |
| Token lifetime | Always expiring, always rotating | Expiring only if the App enables it |
| Reaching a repo DAIV is not installed on | Possible | **Not possible**: the App must be installed |

---

## What this does not defend against

Cross-project access is bounded by *whose* permissions are spent, not by *who chose to spend them*. DAIV reads the issue bodies, comments and repository files of the attached project, and any of those can be written by somebody other than the person the run acts for, including `.agents/AGENTS.md` on a contributor's merge-request branch. Text there can name a project and ask the agent to fetch it.

The consequence is bounded but real. An attacker who can get content into a project DAIV watches, and can get a person with wider access to trigger a run on it, can have that person's token spend reads on a project the attacker cannot reach, and see the result wherever the run reports. The same text can steer the writes that still cross: new issues and comments, and edits to existing issues and merge requests, including their titles and descriptions and, on GitLab, milestone, reviewers and due date. See [What still crosses](#what-still-crosses).

The refusals above stop closing, locking, approving, repointing, assigning (except `gh issue create --assignee`), opening merge and pull requests, driving CI, creating refs and releases, deleting, resetting time tracking, making a confidential issue public, and editing other people's notes. They do not make cross-project access read-only. Every attempt appears in the audit log under that person's name, but the log records that a project was reached, not what was changed, so check the target project itself.

Deployments that cannot accept this should leave the capability off, or narrow the grant to read-only (see [Platform differences](#platform-differences)). Where it is on, keep [webhook-triggered runs](#webhook-triggered-runs) off unless you trust everyone who can open issues, keep DAIV's webhooks on projects whose contributors you trust to open merge requests, and rely on the audit log rather than on the agent's own account of what it did.

---

## Related pages

- [Accounts & Roles](../getting-started/accounts.md): sign-in and OAuth configuration
- [Site Configuration](../reference/site-configuration.md): the switches and the Authentication group
- [Environment Variables](../reference/env-variables.md): `DAIV_GITLAB_OAUTH_SCOPE`, `DAIV_ENCRYPTION_KEY`, `CODEBASE_CROSS_PROJECT_RECORD_RETENTION_DAYS`
