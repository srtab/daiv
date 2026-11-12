---
description: "Connect DAIV to GitLab (GitLab.com or self-hosted) or GitHub so it can address issues, review merge and pull requests, and react to webhooks."
---

# Platform setup

DAIV connects to one Git platform at a time, set by `CODEBASE_CLIENT`: GitLab (GitLab.com or self-managed) or GitHub (GitHub.com or GitHub Enterprise Server). Once connected, DAIV addresses labeled issues and reviews merge and pull requests in the repositories it can reach. Follow [Connect GitLab](#connect-gitlab) or [Connect GitHub](#connect-github).

## Connect GitLab

DAIV works as a GitLab user: it comments, pushes and opens merge requests as the user its token belongs to, and it ignores comments written by that user. Give DAIV its own account. With a token from your own account, DAIV ignores your comments, including the one that approves its plan.

You need:

- DAIV running, from [Deployment](deployment.md).
- Permission to add a member to each project DAIV works on.
- On GitLab.com, a second account for DAIV. On a self-managed instance, an admin to create the user.

1. Create a GitLab user for DAIV, for example `daiv`. On GitLab.com, sign up a second account; on a self-managed instance, an admin creates it under **Admin** → **Users** → **New user**. The username is how you mention DAIV, for example `@daiv`.
2. Add the user to each project DAIV works on, with the **Maintainer** role.
3. Sign in to GitLab as the DAIV user.
4. Select your avatar → **Edit profile** → **Access tokens**.
5. Enter `DAIV Integration` as the token **Name**.
6. Set the expiration according to your security policy.
7. Select the `api` scope.
8. Select **Create personal access token**.

    !!! warning
        GitLab shows the token once. Anyone who holds it acts as DAIV's user in every project that user belongs to. Store it in a secret manager, and never commit it to version control.

9. Copy the token.
10. If Deployment has not set a webhook secret, generate one. It is a random value that GitLab sends with each webhook for DAIV to validate. DAIV registers each webhook with the secret it already holds, so keep an existing one:

    ```bash
    openssl rand -hex 32
    ```

    !!! warning
        A Compose file holds the token and the webhook secret in plain text. Keep it out of version control, and readable only by the users who deploy DAIV.

11. On Docker Compose, in the `environment` of `x-app-defaults` in your `docker-compose.yml`, set `CODEBASE_GITLAB_URL` to your GitLab URL (for example `https://gitlab.com`), `CODEBASE_GITLAB_AUTH_TOKEN` to the token and `CODEBASE_GITLAB_WEBHOOK_SECRET` to the webhook secret.
12. On Docker Swarm, set `CODEBASE_GITLAB_URL` in `x-app-environment-defaults` of `stack.yml`.
13. On Docker Swarm, check whether the `codebase_gitlab_auth_token` secret, which step 1 of [Deploy with Docker Swarm](deployment.md#deploy-with-docker-swarm) created, already holds the token from step 9. If it does, skip to the redeploy step. Otherwise Docker cannot change it, so replace it with the next steps.
14. On Docker Swarm, remove the stack: `docker stack rm daiv`.
15. On Docker Swarm, remove the secret: `docker secret rm codebase_gitlab_auth_token`.

    !!! warning
        A value you type in a command is saved in your shell history and visible in the process list. Run the next command from a shell that does not record history.

16. On Docker Swarm, create the secret again, replacing `<token>`:

    ```bash
    printf '%s' '<token>' | docker secret create codebase_gitlab_auth_token -
    ```

17. Redeploy DAIV so it reads the values: `docker compose up -d` on Compose, or `docker stack deploy -c stack.yml daiv` on Swarm.
18. Wait up to 5 minutes. DAIV's scheduler adds a webhook to each project where its user is a Maintainer or Owner. To add them at once, run [`setup_webhooks`](../reference/management-commands.md#setup_webhooks).
19. In a project, open **Settings** → **Webhooks** and check for a webhook that points to `https://daiv.example.com/api/codebase/callbacks/gitlab/`, with the events in [GitLab token, role and webhooks](#gitlab-token-role-and-webhooks).
20. Select **Test** → **Push events**, and check that GitLab reports a successful delivery.
21. Create an issue titled "Add hello world function" in the project.
22. Add the `daiv` label to the issue. If the project has no `daiv` label, create it from the label picker.
23. Wait a few seconds. DAIV reacts with 👀, then comments with a plan.
24. From your own account, comment on the issue mentioning DAIV's user, for example `@daiv proceed`. DAIV implements the plan and opens a merge request.

To change DAIV's behavior in a repository, add a `.daiv.yml` file to its root; [Repository configuration](../customization/repository-config.md) lists the options. [Issue addressing](../features/issue-addressing.md) explains what DAIV does with an issue.

## GitLab token, role and webhooks

| Item | Value |
|------|-------|
| Token | A personal access token of DAIV's user, with the `api` scope. |
| Role | DAIV lists only the projects where its user is a Maintainer or Owner, and sets up webhooks on those. |
| Webhook URL | `<DAIV_EXTERNAL_URL>/api/codebase/callbacks/gitlab/`. GitLab must be able to reach `DAIV_EXTERNAL_URL`. |
| Webhook events | Push events, Issues events, Comments (Note events), Merge request events and Pipeline events. |
| Webhook creation | DAIV's scheduler runs `setup_webhooks` on the `CODEBASE_WEBHOOK_SETUP_CRON` schedule, every 5 minutes by default. See [Management commands](../reference/management-commands.md#setup_webhooks) and [Environment variables](../reference/env-variables.md). |

## Troubleshoot GitLab

### Webhook delivery fails

The webhook URL is not reachable from GitLab, or the certificate is invalid. Check that `DAIV_EXTERNAL_URL` is reachable from GitLab, that its SSL certificate is valid, and that no firewall blocks GitLab.

A self-managed GitLab refuses webhooks to private network addresses by default, with the error `Url is blocked: Requests to the local network are not allowed`. An admin allows them under **Admin** → **Settings** → **Network** → **Outbound requests**.

### DAIV ignores an issue

- The issue has no `daiv` label. Add it.
- DAIV's user is not a Maintainer or Owner of the project, so the project has no webhook. Change its role.
- `issue_addressing.enabled` is `false` in `.daiv.yml`. It is `true` by default.
- Check DAIV's logs for errors.

### DAIV ignores your comments

- DAIV ignores comments written by its own user. Comment from another account.
- Mention DAIV's username, for example `@daiv`.

### DAIV does not respond to merge request comments

- The webhook does not send **Comments (Note events)**. Run `setup_webhooks --update`.
- The webhook secret in GitLab does not match `CODEBASE_GITLAB_WEBHOOK_SECRET`. DAIV rejects the request.
- DAIV's user lacks access to the project.

### DAIV reports an error in a comment

DAIV comments "An unexpected error occurred" when a run fails. The worker and sandbox logs show why, for example `docker compose logs worker sandbox`.

## Connect GitHub

DAIV authenticates to GitHub as a GitHub App, which you create and install on the repositories DAIV works on. You need:

- DAIV running, from [Deployment](deployment.md).
- Permission to create and install a GitHub App on your account or organization.

1. Open **Settings** → **Developer settings** → **GitHub Apps** in your account, or in **Organization Settings** for an organization.
2. Select **New GitHub App**.
3. Enter a unique name as the **GitHub App name**, for example `DAIV - <org-name>`, replacing `<org-name>` with your organization or account name. GitHub rejects a name another App uses.
4. Enter your DAIV URL, for example `https://daiv.example.com`, as the **Homepage URL**.
5. Enter `https://daiv.example.com/api/codebase/callbacks/github/` as the **Webhook URL**.
6. Generate the webhook secret, and keep it for a later step:

    ```bash
    openssl rand -hex 32
    ```

7. Enter the webhook secret as the **Webhook secret**.

    !!! warning
        The **Workflows** permission is optional, and it lets code that runs in the sandbox change files under `.github/workflows/`. Those workflows run with the repository's Actions secrets, which exposes the secrets to that code. Set it only if DAIV must push to those files.

8. Set the repository permissions listed in [GitHub App permissions and events](#github-app-permissions-and-events).
9. Subscribe the App to the events listed there.
10. Select **Create GitHub App**.

    !!! warning
        The private key lets whoever holds it act as the App on every installed repository. GitHub offers the `.pem` file once, so store it where only DAIV's operators can read it.

11. Under **Private keys**, select **Generate a private key**. Your browser downloads a `.pem` file.
12. Copy the **App ID** from the top of the App's settings page.
13. Select **Install App** in the left sidebar.
14. Select your account or organization.
15. Select **All repositories** or **Only select repositories**, and choose the repositories DAIV works on.
16. Select **Install**.
17. Copy the installation ID from the page's URL, `https://github.com/settings/installations/<installation-id>`.
18. On Docker Compose, in the `environment` of `x-app-defaults` in your `docker-compose.yml`, set `CODEBASE_CLIENT: github`, `CODEBASE_GITHUB_APP_ID` to the App ID and `CODEBASE_GITHUB_INSTALLATION_ID` to the installation ID. For GitHub Enterprise Server, also set `CODEBASE_GITHUB_URL`, for example `https://github.example.com`.

    !!! warning
        A Compose file holds the webhook secret in plain text. Keep it out of version control, and readable only by the users who deploy DAIV.

19. On Docker Compose, set `CODEBASE_GITHUB_WEBHOOK_SECRET` in the same `environment` to the webhook secret.
20. On Docker Compose, add the private key as a file-based secret. Under `x-app-defaults`, add `secrets: [codebase_github_private_key]`. At the top level, add:

    ```yaml
    secrets:
      codebase_github_private_key:
        file: <private-key-path>
    ```

    Replace `<private-key-path>` with the path of the `.pem` file. DAIV reads it from `/run/secrets/codebase_github_private_key`. The `secrets:` key in `x-app-defaults` reaches only the services that merge that anchor with `<<: *x_app_default`, which the [Compose file](deployment.md#compose-file) does for `app`, `worker`, `worker-interactive` and `scheduler`.
21. On Docker Compose, give the `.pem` file to the user DAIV runs as, `daiv` with UID 1001, because Compose keeps the file's owner and permissions. Replace `<private-key-path>` with the path of the `.pem` file:

    ```bash
    sudo chown 1001:1001 <private-key-path>
    ```

22. On Docker Compose, make the file readable by that user only:

    ```bash
    chmod 600 <private-key-path>
    ```

23. On Docker Swarm, in `x-app-environment-defaults` of `stack.yml`, set `CODEBASE_CLIENT: github`, `CODEBASE_GITHUB_APP_ID` and `CODEBASE_GITHUB_INSTALLATION_ID`. For GitHub Enterprise Server, also set `CODEBASE_GITHUB_URL`.

    !!! warning
        A value you type in a command is saved in your shell history and visible in the process list. Run the next command from a shell that does not record history, or from a script that reads the value from a protected file.

24. On Docker Swarm, create the webhook secret, replacing `<webhook-secret>`:

    ```bash
    printf '%s' '<webhook-secret>' | docker secret create codebase_github_webhook_secret -
    ```

25. On Docker Swarm, create the private key secret from the `.pem` file, replacing `<private-key-path>`:

    ```bash
    docker secret create codebase_github_private_key <private-key-path>
    ```

26. On Docker Swarm, add `codebase_github_private_key` and `codebase_github_webhook_secret` to the `secrets:` list of `app`, `worker`, `worker-interactive` and `scheduler`, and declare both under the top-level `secrets:` with `external: true`, as the stack file does for the other secrets.
27. Redeploy DAIV so it reads the values: `docker compose up -d` on Compose, or `docker stack deploy -c stack.yml daiv` on Swarm.
28. Create an issue titled "Add hello world function" in a repository where you installed the App.
29. Add the `daiv` label to the issue. If the repository has no `daiv` label, create it.
30. Wait a few seconds. DAIV comments with a plan.
31. In your GitHub App's settings, open **Advanced** → **Recent Deliveries**, and check that the `issues` delivery for the test issue shows a `204` response. The `ping` and `installation` deliveries from before DAIV was configured show an error, and you can ignore them.

To change DAIV's behavior in a repository, add a `.daiv.yml` file to its root; [Repository configuration](../customization/repository-config.md) lists the options. [Issue addressing](../features/issue-addressing.md) explains what DAIV does with an issue.

## GitHub App permissions and events

GitHub sends the events of every repository that has the App installed to the one webhook set on the App. DAIV does not create webhooks on GitHub, and [`setup_webhooks`](../reference/management-commands.md#setup_webhooks) does nothing there.

| Repository permission | Access | DAIV uses it to |
|-----------------------|--------|-----------------|
| Contents | Read & Write | Create branches and commits. |
| Issues | Read & Write | Read and comment on issues. |
| Pull requests | Read & Write | Create and update pull requests. |
| Actions | Read-only | Read workflow runs and job logs for [pipeline watch](../features/pipeline-watch.md). |
| Workflows | Read & Write (optional) | Push changes to files under `.github/workflows/`, such as a conflict resolved there. |
| Metadata | Read-only | GitHub selects it for you. |

Subscribe the App to these events:

- **Push**
- **Issues**
- **Issue comment**
- **Pull request review comment**, for mentions and comments on diff lines of a pull request a DAIV session owns
- **Pull request**
- **Workflow run**, which GitHub lists only once **Actions** is **Read-only**

If you grant a new permission to an App that is already installed, each installation owner must accept it. Until they do, the features that need it do not run on that installation.

## Troubleshoot GitHub

### Webhook delivery fails

- Check that `DAIV_EXTERNAL_URL` is reachable from GitHub and has a valid SSL certificate.
- Check that the **Webhook secret** in the GitHub App matches `CODEBASE_GITHUB_WEBHOOK_SECRET`. Without a match, DAIV answers 401.
- Check that `CODEBASE_CLIENT` is `github`. Otherwise DAIV answers 403.
- A `ping` or `installation` delivery fails even when DAIV is configured, because it has no repository. DAIV answers 422. Check the `issues` delivery instead.
- Check DAIV's logs for the incoming requests.

### DAIV ignores an issue

- The issue has no `daiv` label. Add it.
- The App lacks the **Issues** permission with **Read & Write** access.
- `issue_addressing.enabled` is `false` in `.daiv.yml`. It is `true` by default.
- Check DAIV's logs for errors.

### DAIV does not respond to comments

- The App is not subscribed to **Issue comment**, **Pull request review comment** or both. See [GitHub App permissions and events](#github-app-permissions-and-events).
- The App is not installed on the repository.

### DAIV fails to start with permission denied on the private key

The log shows `permission denied` on `/run/secrets/codebase_github_private_key`. The `.pem` file is not readable by UID 1001. Run `sudo chown 1001:1001 <private-key-path>` and `chmod 600 <private-key-path>`, then restart DAIV.

### Authentication fails

- The private key is not in PEM format, or lacks its header and footer lines.
- `CODEBASE_GITHUB_APP_ID` or `CODEBASE_GITHUB_INSTALLATION_ID` is wrong.
- The App is not installed on the repositories DAIV works on.

## Next steps

- **[LLM providers](llm-providers.md)** configures your LLM provider and API keys.
- **[Repository configuration](../customization/repository-config.md)** customizes DAIV's behavior per repository.
- **[Issue addressing](../features/issue-addressing.md)** explains how DAIV handles issues.
- **[Pull request assistant](../features/pull-request-assistant.md)** explains how DAIV reviews code.
