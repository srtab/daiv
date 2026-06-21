# Accounts & Roles

DAIV's web dashboard requires you to sign in. Every authenticated user has one of two roles — **admin** or **member** — which decide what they can see and change. Members manage their own work (agent runs, chats, schedules, and API keys); admins additionally manage the deployment: site configuration, users, global skills, and global sandbox environments.

Self-service signup is **disabled**. Users either sign in with their Git platform account (when OAuth is enabled) or are pre-created by an admin and sign in with a one-time email code.

## Roles

DAIV ships exactly two roles. New users default to **member**.

| Role       | Can do                                                                                                                                                                                                                                                                                        |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Member** | Start and view their own agent runs; use the dashboard chat workspace; create and manage their own [scheduled jobs](https://srtab.github.io/daiv/dev/features/scheduled-jobs/index.md); create and revoke their own API keys; manage their own notification channels.                         |
| **Admin**  | Everything a member can, plus: edit **Site Configuration** at `/dashboard/configuration/`; create, edit, and delete users at `/accounts/users/`; manage global (shared) skills and global sandbox environments; view system-wide velocity metrics and the full API-key list on the dashboard. |

How role checks work

Admin-only pages are guarded by `AdminRequiredMixin`, which raises *permission denied* for any signed-in user whose role is not `admin`. In code this is the `user.is_admin` property. There are no other privilege levels — a user is either an admin or a member.

## Signing in

The login page lives at `/accounts/login/`. Two sign-in methods are available, depending on how the deployment is configured.

### Git platform login (OAuth)

When OAuth login is enabled, users can sign in with the same Git platform DAIV is connected to — **GitHub** or **GitLab** (including self-hosted GitLab). DAIV offers the provider that matches your configured platform; it does not offer both at once.

OAuth is configured by an admin in **Site Configuration** under the **Authentication** section, not in code. The relevant settings are:

| Setting                                       | Purpose                                                                                                                    |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **Enable OAuth login** (`auth_login_enabled`) | Master toggle. When off, no social provider is offered on the login page.                                                  |
| **Open social signup** (`auth_signup_open`)   | When on, anyone who authenticates via your Git platform gets an account. When off, only pre-registered emails can sign in. |
| **OAuth client ID** / **OAuth client secret** | The OAuth application credentials for your platform. Set both, or neither.                                                 |
| **GitLab URL**                                | Browser-facing URL of your GitLab instance (GitLab only).                                                                  |
| **GitLab server URL**                         | Optional server-to-server URL for GitLab API calls inside a Docker-internal network. Leave empty to reuse the GitLab URL.  |

Tip

OAuth can be toggled on and off at any time from `/dashboard/configuration/` — you do not need to redeploy. The same credentials can also be seeded from the `ALLAUTH_CLIENT_ID`, `ALLAUTH_CLIENT_SECRET`, `ALLAUTH_GITLAB_URL`, and `ALLAUTH_GITLAB_SERVER_URL` environment variables. See [Authentication in the env-variables reference](https://srtab.github.io/daiv/dev/reference/env-variables/#authentication) and [Deployment](https://srtab.github.io/daiv/dev/getting-started/deployment/index.md).

### Email login-by-code

Users can also sign in **without a password** using a one-time code emailed to them:

1. On the login page, request a login code for your email address.
1. DAIV emails a short, single-use code.
1. Enter the code to complete sign-in.

This is the path used by admin-created users and by the bootstrapped first admin — it works even when OAuth is not configured, as long as DAIV can send email.

No password signup

Standard email-and-password registration is turned off (`AccountAdapter.is_open_for_signup` returns `False`). Visiting the signup URL simply redirects back to the login page. New accounts come only from OAuth (when open signup is on) or from an admin creating them.

## The first admin

A fresh DAIV install has no users, so it has no admin. There are two ways to establish the first one.

### First OAuth sign-in becomes admin

On a fresh install with OAuth enabled, the **first user to sign in via a social provider is automatically promoted to admin**. The trigger is the *absence of any admin user* (not the absence of any user), so if two people happen to sign in at the same moment, the result is multiple admins (safe) rather than none.

### Bootstrap command (headless)

When OAuth is not yet configured, create the initial admin from a shell:

```
docker exec -it daiv-app python manage.py bootstrap_admin admin@example.com
```

The `bootstrap_admin` command creates an admin user and prints how to sign in. It is a one-shot bootstrap: it refuses to run if an admin already exists, and refuses if a user with that email already exists (promote that user via the dashboard instead). The new admin signs in via login-by-code (the one-time email code).

Info

For end-to-end first-run setup including OAuth credentials and email, see the first-admin notes in [Deployment](https://srtab.github.io/daiv/dev/getting-started/deployment/index.md).

## Managing users (admin only)

Admins manage accounts at `/accounts/users/`. The list supports searching by name, email, or username and filtering by role.

- **Create a user**

  ______________________________________________________________________

  At `/accounts/users/create/`, set the user's **name**, **email**, and **role**. DAIV emails a welcome message with a sign-in link. The new account has no password — the user signs in via OAuth or login-by-code.

- **Edit a user**

  ______________________________________________________________________

  At `/accounts/users/<id>/edit/`, change **name**, **email**, **role**, and **active** status. Use this to promote a member to admin or demote an admin to member.

- **Delete a user**

  ______________________________________________________________________

  At `/accounts/users/<id>/delete/`, permanently remove an account after confirmation.

DAIV enforces a few guardrails so a deployment can never be locked out of administration:

- **You cannot delete your own account** from the user-management screen.
- **The last active admin cannot be deleted, deactivated, or demoted.** Promote another user to admin first — the guard is `user.is_last_active_admin()`, applied on delete and on role/active changes.

Deactivate vs. delete

Setting a user to inactive blocks them from signing in while preserving their history and ownership of runs. Deleting removes the account outright. Prefer deactivation when you may want to restore access later.

## Per-user settings

Every signed-in user — member or admin — has these self-service pages:

| Page                      | Route                 | What it does                                                                                              |
| ------------------------- | --------------------- | --------------------------------------------------------------------------------------------------------- |
| **Dashboard**             | `/dashboard/`         | Your activity summary and quick links. Admins also see system-wide velocity metrics and total user count. |
| **API keys**              | `/accounts/api-keys/` | Create and revoke personal API keys for programmatic access.                                              |
| **Notification channels** | `/accounts/channels/` | Choose how you're notified about your agent runs (in-app, email, and Rocket.Chat when enabled).           |

API keys authenticate calls to the [Jobs API](https://srtab.github.io/daiv/dev/features/jobs-api/index.md) and the [MCP endpoint](https://srtab.github.io/daiv/dev/features/mcp-endpoint/index.md). Members see only their own keys; admins see every user's keys in the list.

## Related pages

- **Deployment**

  ______________________________________________________________________

  Install DAIV, configure email, and bring up the first admin.

  [Deployment](https://srtab.github.io/daiv/dev/getting-started/deployment/index.md)

- **Platform Setup**

  ______________________________________________________________________

  Connect DAIV to GitLab or GitHub — the same platform used for OAuth login.

  [Platform Setup](https://srtab.github.io/daiv/dev/getting-started/platform-setup/index.md)

- **Jobs API**

  ______________________________________________________________________

  Authenticate programmatic agent runs with a personal API key.

  [Jobs API](https://srtab.github.io/daiv/dev/features/jobs-api/index.md)
