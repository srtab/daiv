---
description: "How sign-in works in the DAIV dashboard: admin and member roles, Git platform OAuth, one-time email codes or passkeys, the first admin, and managing users."
---

# Accounts and roles

DAIV's web dashboard requires you to sign in. Every signed-in user has one of two roles, **admin** or **member**, which decide what they can see and change. Members manage their own work: agent runs, chats, schedules and API keys. Admins also manage the deployment: users, site configuration, schedule templates, skills, and global MCP servers and sandbox environments.

DAIV does not offer email-and-password signup. Signup through your Git platform is off by default: an admin turns it on with **Open social signup**, described in [Turn on OAuth login](../reference/site-configuration.md#turn-on-oauth-login). Otherwise an admin creates each user, who then signs in with a one-time email code, a passkey, or the Git platform account when OAuth login is on.

## Roles

DAIV has two roles. New users are members.

| Capability | Member | Admin |
|------------|--------|-------|
| Start and view agent runs and chats | On the repositories they can access (see [Repository access](#repository-access)) | On every repository |
| Create and view [scheduled jobs](../features/scheduled-jobs.md) | On the repositories they can access | On every repository |
| Create and revoke API keys | Their own | Their own, and every other user's key in the list |
| Manage notification channels | Their own | Their own |
| **Users**: create, edit and delete users | No | Yes |
| **Configuration**: edit the site configuration | No | Yes |
| **Schedule templates**: create, edit and delete them | No | Yes |
| **Skills**: open the skills dashboard | No | Yes |
| **Global MCP servers**: manage the servers every user gets | No | Yes |
| Create and change global sandbox environments | No | Yes |
| Consolidate a repository's memory on demand | No | Yes |

The admin-only pages are under **Admin** in the dashboard sidebar.

## Repository access

A member sees and acts on a repository only when they have access to it on the connected Git platform, GitLab or GitHub. DAIV mirrors each user's repository membership from the platform and checks it before it shows or runs anything on a repository. There is no separate DAIV-side permission to manage. Admins skip these checks.

DAIV checks two tiers, matched to the platform's own roles:

| Tier | Required platform role | Gates |
|------|------------------------|-------|
| **Read** | GitLab Reporter or above, GitHub read (or higher) collaborator | Repository and branch pickers, the memory dashboard, and the MCP `list_repositories` tool. |
| **Write** | GitLab Developer or above, GitHub write (or higher) collaborator | Starting an agent run or chat, and creating or running a [scheduled job](../features/scheduled-jobs.md), from the dashboard, the Jobs API or MCP. |

A repository a user cannot read never appears in a listing. A repository they can read but not write to appears, and DAIV rejects their run, chat and schedule attempts with a "not accessible" message.

DAIV learns what a user can access on the platform only after that user signs in with their Git platform account once, and matches the platform account to the DAIV user by email. A user an admin created, who has only ever signed in with an email code, has no repository access until they sign in with GitLab or GitHub once.

A periodic sync copies repository membership from the platform, so a permission change on the platform takes effect at the next sync, not immediately. [Git platform](../reference/env-variables.md#git-platform) gives the sync schedule and how long DAIV keeps trusting access data after a sync fails.

## Sign in

The login page is at `/accounts/login/`. A user signs in with any of these methods:

- **Email code.** Works without further setup, as long as DAIV can send email.
- **Git platform account.** Works once an admin has turned on OAuth login.
- **Passkey.** Works once the user has added one.

### Sign in with an email code

DAIV emails a one-time code that signs a user in without a password. Admin-created users and the first admin use this method.

1. On the login page, enter your email address and select **Continue with email**.
2. Read the code in the email DAIV sends. The code expires after 5 minutes and accepts 3 attempts.
3. Enter the code and select **Verify & sign in**.

Without an SMTP server, set `EMAIL_BACKEND` to `django.core.mail.backends.console.EmailBackend` and read the code from the app's log with `docker compose logs app`. [Environment variables](../reference/env-variables.md#authentication) lists the email settings.

### Sign in with GitHub or GitLab

DAIV offers the platform it is connected to, **GitHub** or **GitLab** including a self-managed GitLab, and never both at once. On the login page, select **Continue with GitHub** or **Continue with GitLab**.

With **Open social signup** off, create each permitted user under **Admin → Users** with the email address their Git platform account confirms. For GitLab, DAIV checks the profile's `confirmed_at` to verify that this primary email address is confirmed before it looks up an existing account. Matching ignores case and keeps the existing user's role. If GitLab sends no confirmation or an invalid one, DAIV does not link the account by email. An account already linked to a platform user ID stays linked when the platform email changes.

DAIV checks again, before it saves a new account, that signup is open. A pending signup form cannot create a second user by replacing an email address that already exists.

To turn on Git platform sign-in, [create the OAuth app](#create-the-oauth-app), then follow [Turn on OAuth login](../reference/site-configuration.md#turn-on-oauth-login).

### Sign in with a passkey

A passkey is Touch ID, Face ID, Windows Hello or a security key. After a user signs in, they add one under **Passkeys** in the account menu at the bottom of the sidebar. On the login page they then select **Sign in with a passkey**. Email sign-in stays available, so a lost passkey does not lock the user out.

### Create the OAuth app

Create an OAuth app on your Git platform to get the client ID and client secret that DAIV needs for OAuth login. You need an account that can create the app on that platform, and the URL DAIV is served from (`DAIV_EXTERNAL_URL`), for example `https://daiv.example.com`.

To create the app on GitHub:

1. Open [github.com/settings/developers](https://github.com/settings/developers).
2. Select **OAuth Apps → New OAuth App**.
3. In **Application name**, enter a name, for example `DAIV`.
4. In **Homepage URL**, enter your DAIV URL, for example `https://daiv.example.com`.
5. In **Authorization callback URL**, enter your DAIV URL followed by `/accounts/github/login/callback/`, for example `https://daiv.example.com/accounts/github/login/callback/`.
6. Select **Register application**.
7. Copy the **Client ID**.
8. Select **Generate a new client secret**, and copy the secret.

To create the app on GitLab:

1. Open **Admin Area → Applications** or **User Settings → Applications**.
2. Add a new application.
3. In **Name**, enter a name, for example `DAIV`.
4. In **Redirect URI**, enter your DAIV URL followed by `/accounts/gitlab/login/callback/`, for example `https://daiv.example.com/accounts/gitlab/login/callback/`.
5. Under **Scopes**, select `read_user`.
6. Select **Save application**.
7. Copy the **Application ID** and the **Secret**.

Enter the client ID and secret under **Configuration → Authentication**, where DAIV calls the secret **Auth Client Secret**: [Turn on OAuth login](../reference/site-configuration.md#turn-on-oauth-login) gives the steps, and [Authentication](../reference/env-variables.md#authentication) lists the variables that seed the same values.

## The first admin

A new DAIV install has no users, so it has no admin. Two paths create the first one: the first Git platform sign-in, and a command in a shell.

### Make the first OAuth sign-in the admin

With OAuth login on and no user in DAIV, the first person who signs in with the Git platform account gets an account even when **Open social signup** is off, and DAIV makes them an admin. DAIV makes a new account an admin whenever the install has no admin, not whenever it has no user. If two people sign in at the same moment, both become admins rather than neither.

### Create the first admin from a shell

Use this path when OAuth login is off. The [`bootstrap_admin`](../reference/management-commands.md#bootstrap_admin) command refuses to run if an admin already exists, or if a user with that email already exists: promote that user from **Users** instead.

1. Run the command, replacing `admin@example.com` with the admin's email address:

    ```bash
    docker compose exec app django-admin bootstrap_admin admin@example.com
    ```

2. [Sign in with an email code](#sign-in-with-an-email-code) as that address.

[Deployment](deployment.md) covers the first-run setup, including email.

## Manage users

Admins manage users under **Admin → Users**, at `/accounts/users/`. The list searches by name or email address and filters by role.

### Create a user

1. Under **Admin → Users**, select **Create user**.
2. In **Name**, **Email** and **Role**, enter the user's details.
3. Select **Create user**.

DAIV emails the user a welcome message with a sign-in link. The new account has no password: the user signs in with an email code, a passkey, or the Git platform account.

### Edit a user

1. Under **Admin → Users**, select the user.
2. Change **Name**, **Email**, **Role** or **Active**. Change **Role** to promote a member to admin or demote an admin to member.
3. Select **Save changes**.

DAIV keeps at least one active admin. You cannot deactivate your own account, and you cannot demote or delete the last active admin: promote another user first.

### Deactivate a user

Clear **Active** on the user's edit page when you may want to restore the user's access later. An inactive user cannot sign in, and their API keys and MCP tokens stop working at once, not at the next sync. DAIV keeps their history and the runs they own.

### Delete a user

Delete a user only when you will never restore their access. Deactivating keeps their history and ownership of runs: see [Deactivate a user](#deactivate-a-user).

!!! warning "Deleting a user cannot be undone"
    Deleting removes the user's account, their API keys and all data associated with them. You cannot delete your own account.

1. Under **Admin → Users**, select the user.
2. Select **Delete user**.
3. On the confirmation page, select **Delete user**.

## Account pages

Every signed-in user, member or admin, has these pages. All but **Home** open from the account menu at the bottom of the sidebar.

| Page | Route | What it does |
|------|-------|--------------|
| **Home** | `/dashboard/` | Your landing page after sign-in, with a link to **Team insights**. |
| **API keys** | `/accounts/api-keys/` | Create and revoke personal API keys for programmatic access. |
| **Notification channels** | `/accounts/channels/` | Choose how DAIV notifies you about your agent runs. [Notifications](../features/notifications.md) lists the channels. |
| **Passkeys** | `/accounts/mfa/` | Add and remove the passkeys you sign in with. |

API keys authenticate calls to the [Jobs API](../features/jobs-api.md) and the [MCP endpoint](../features/mcp-endpoint.md). A member sees only their own keys, and an admin sees every user's keys in the list.

## Related pages

<div class="grid cards" markdown>

-   **Deployment**

    ---

    Install DAIV, configure email, and create the first admin.

    [:octicons-arrow-right-24: Deployment](deployment.md)

-   **Platform Setup**

    ---

    Connect DAIV to GitLab or GitHub, the same platform used for OAuth login.

    [:octicons-arrow-right-24: Platform Setup](platform-setup.md)

-   **Jobs API**

    ---

    Authenticate programmatic agent runs with a personal API key.

    [:octicons-arrow-right-24: Jobs API](../features/jobs-api.md)

</div>
