# Accounts & Roles

DAIV's web dashboard requires you to sign in. Every authenticated user has one of two roles — **admin** or **member** — which decide what they can see and change. Members manage their own work (agent runs, chats, schedules, and API keys); admins additionally manage the deployment: site configuration, users, global skills, and global sandbox environments.

Self-service signup is **disabled**. Users either sign in with their Git platform account (when OAuth is enabled) or are pre-created by an admin and sign in with a one-time email code.

## Roles

DAIV ships exactly two roles. New users default to **member**.

| Role | Can do |
|------|--------|
| **Member** | Start and view agent runs, chat, and [scheduled jobs](../features/scheduled-jobs.md) — scoped to the repositories they can access on the connected Git platform (see [Repository access](#repository-access)); create and revoke their own API keys; manage their own notification channels. |
| **Admin** | Everything a member can, but without the repository restriction — admins see and act on every repository. Plus: edit **Site Configuration** at `/dashboard/configuration/`; create, edit, and delete users at `/accounts/users/`; manage global (shared) skills and global sandbox environments; view system-wide velocity metrics and the full API-key list on the dashboard. |

!!! note "How role checks work"
    Admin-only pages are guarded by `AdminRequiredMixin`, which raises *permission denied* for any signed-in user whose role is not `admin`. In code this is the `user.is_admin` property. There are no other privilege levels — a user is either an admin or a member.

## Repository access

Being a member does not, by itself, grant access to any repository. DAIV mirrors each user's repository membership from the connected Git platform (GitLab or GitHub) and enforces it before letting the user see or act on a repository — there is no separate DAIV-side permission to manage.

Two access tiers are checked, matched to the platform's own roles:

| Tier | Required platform role | Gates |
|------|------------------------|-------|
| **Read** | GitLab Reporter or above, GitHub read (or higher) collaborator | Repository/branch pickers, repository search, the memory dashboard, and the MCP `list_repositories` tool. |
| **Write** | GitLab Developer or above, GitHub write (or higher) collaborator | Starting an agent run or chat, and creating or running a [scheduled job](../features/scheduled-jobs.md) — from the dashboard, the Jobs API, or MCP. |

A repository the user can't read never appears in a listing; one they can read but not write to appears but rejects run/chat/schedule attempts with a friendly "not accessible" message rather than a stack trace.

**Access requires a verified platform identity.** DAIV only knows what a user can access on GitLab/GitHub once that user has signed in via OAuth at least once (see [Git platform login](#git-platform-login-oauth)) — the platform account is matched to the DAIV user by email. A user created by an admin who has only ever used email login-by-code has no repository access until they connect their GitLab/GitHub account by logging in with it once.

**Admins bypass all repository checks** — the pre-existing "admin sees everything" behavior is unchanged.

Access data is refreshed by a periodic background sync (every 15 minutes by default, `CODEBASE_REPO_ACCESS_SYNC_CRON`), so a permission change on the Git platform (added, removed, or role-changed) takes effect on the next sync rather than immediately. Freshness is tracked **per repository**: if a repository's sync stops succeeding, its access data is served from the last known-good data for a grace period, and beyond `CODEBASE_REPO_ACCESS_HARD_TTL_HOURS` (24 hours by default) that repository's member access **fails closed** rather than trusting stale grants — repositories that keep syncing cleanly are unaffected. Admin access is never affected by sync health.

!!! note "Deactivation revokes access immediately"
    Setting a user to inactive (see [Managing users](#managing-users-admin-only)) immediately blocks that user's API keys and MCP tokens, in addition to signing them out of the dashboard — outstanding credentials stop working right away rather than at the next sync.

## Signing in

The login page lives at `/accounts/login/`. Two sign-in methods are available, depending on how the deployment is configured.

### Git platform login (OAuth)

When OAuth login is enabled, users can sign in with the same Git platform DAIV is connected to — **GitHub** or **GitLab** (including self-hosted GitLab). DAIV offers the provider that matches your configured platform; it does not offer both at once.

OAuth is configured by an admin in **Site Configuration** under the **Authentication** section, not in code. The relevant settings are:

| Setting | Purpose |
|---------|---------|
| **Enable OAuth login** (`auth_login_enabled`) | Master toggle. When off, no social provider is offered on the login page. |
| **Open social signup** (`auth_signup_open`) | When on, anyone who authenticates via your Git platform gets an account. When off, only pre-registered emails can sign in. |
| **OAuth client ID** / **OAuth client secret** | The OAuth application credentials for your platform. Set both, or neither. |
| **GitLab URL** | Browser-facing URL of your GitLab instance (GitLab only). |
| **GitLab server URL** | Optional server-to-server URL for GitLab API calls inside a Docker-internal network. Leave empty to reuse the GitLab URL. |

!!! tip
    OAuth can be toggled on and off at any time from `/dashboard/configuration/` — you do not need to redeploy. The same credentials can also be seeded from the `ALLAUTH_CLIENT_ID`, `ALLAUTH_CLIENT_SECRET`, `ALLAUTH_GITLAB_URL`, and `ALLAUTH_GITLAB_SERVER_URL` environment variables. See [Authentication in the env-variables reference](../reference/env-variables.md#authentication) and [Deployment](deployment.md).

### Email login-by-code

Users can also sign in **without a password** using a one-time code emailed to them:

1. On the login page, request a login code for your email address.
2. DAIV emails a short, single-use code.
3. Enter the code to complete sign-in.

This is the path used by admin-created users and by the bootstrapped first admin — it works even when OAuth is not configured, as long as DAIV can send email.

!!! warning "No password signup"
    Standard email-and-password registration is turned off (`AccountAdapter.is_open_for_signup` returns `False`). Visiting the signup URL simply redirects back to the login page. New accounts come only from OAuth (when open signup is on) or from an admin creating them.

## The first admin

A fresh DAIV install has no users, so it has no admin. There are two ways to establish the first one.

### First OAuth sign-in becomes admin

On a fresh install with OAuth enabled, the **first user to sign in via a social provider is automatically promoted to admin**. The trigger is the *absence of any admin user* (not the absence of any user), so if two people happen to sign in at the same moment, the result is multiple admins (safe) rather than none.

### Bootstrap command (headless)

When OAuth is not yet configured, create the initial admin from a shell:

```bash
docker exec -it daiv-app python manage.py bootstrap_admin admin@example.com
```

The `bootstrap_admin` command creates an admin user and prints how to sign in. It is a one-shot bootstrap: it refuses to run if an admin already exists, and refuses if a user with that email already exists (promote that user via the dashboard instead). The new admin signs in via login-by-code (the one-time email code).

!!! info
    For end-to-end first-run setup including OAuth credentials and email, see the first-admin notes in [Deployment](deployment.md).

## Managing users (admin only)

Admins manage accounts at `/accounts/users/`. The list supports searching by name, email, or username and filtering by role.

<div class="grid cards" markdown>

-   :octicons-person-add-16: **Create a user**

    ---

    At `/accounts/users/create/`, set the user's **name**, **email**, and **role**. DAIV emails a welcome message with a sign-in link. The new account has no password — the user signs in via OAuth or login-by-code.

-   :octicons-pencil-16: **Edit a user**

    ---

    At `/accounts/users/<id>/edit/`, change **name**, **email**, **role**, and **active** status. Use this to promote a member to admin or demote an admin to member.

-   :octicons-trash-16: **Delete a user**

    ---

    At `/accounts/users/<id>/delete/`, permanently remove an account after confirmation.

</div>

DAIV enforces a few guardrails so a deployment can never be locked out of administration:

- **You cannot delete your own account** from the user-management screen.
- **The last active admin cannot be deleted, deactivated, or demoted.** Promote another user to admin first — the guard is `user.is_last_active_admin()`, applied on delete and on role/active changes.

!!! note "Deactivate vs. delete"
    Setting a user to inactive blocks them from signing in while preserving their history and ownership of runs. Deleting removes the account outright. Prefer deactivation when you may want to restore access later.

## Per-user settings

Every signed-in user — member or admin — has these self-service pages:

| Page | Route | What it does |
|------|-------|--------------|
| **Dashboard** | `/dashboard/` | Your activity summary and quick links. Admins also see system-wide velocity metrics and total user count. |
| **API keys** | `/accounts/api-keys/` | Create and revoke personal API keys for programmatic access. |
| **Notification channels** | `/accounts/channels/` | Choose how you're notified about your agent runs (in-app, email, and Rocket.Chat when enabled). |

API keys authenticate calls to the [Jobs API](../features/jobs-api.md) and the [MCP endpoint](../features/mcp-endpoint.md). Members see only their own keys; admins see every user's keys in the list.

## Related pages

<div class="grid cards" markdown>

-   **Deployment**

    ---

    Install DAIV, configure email, and bring up the first admin.

    [:octicons-arrow-right-24: Deployment](deployment.md)

-   **Platform Setup**

    ---

    Connect DAIV to GitLab or GitHub — the same platform used for OAuth login.

    [:octicons-arrow-right-24: Platform Setup](platform-setup.md)

-   **Jobs API**

    ---

    Authenticate programmatic agent runs with a personal API key.

    [:octicons-arrow-right-24: Jobs API](../features/jobs-api.md)

</div>
