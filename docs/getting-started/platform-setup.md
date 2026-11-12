---
description: "Connect DAIV to GitLab (GitLab.com or self-hosted) or GitHub so it can address issues, review merge and pull requests, and react to webhooks."
---

# Platform Setup

This guide walks you through connecting DAIV to your GitLab or GitHub repository. Once configured, DAIV will automatically address issues, review pull requests, and more.

DAIV supports both platforms:

- **[GitLab](#gitlab-configuration)** — GitLab.com or self-hosted GitLab instances
- **[GitHub](#github-configuration)** — GitHub.com or GitHub Enterprise

---

## GitLab Configuration

### Prerequisites

Before configuring a GitLab repository, ensure you have:

- **DAIV installed and running** - Follow the [Deployment](deployment.md) guide first
- **GitLab repository access** - Admin or maintainer permissions on the repository you want to connect
- **A GitLab account for DAIV** - A separate user that DAIV acts as, created in Step 1

### Step 1: Create a GitLab User and Token for DAIV

DAIV works as a GitLab user: it comments, pushes and opens merge requests as the user its token belongs to, and it ignores comments written by that user. Give DAIV its own account. With a token from your own account, DAIV ignores your comments, including the one that approves its plan.

1. **Create the user**:

    - Create a GitLab user for DAIV, for example `daiv`. Its username is how you mention DAIV (`@daiv`). On GitLab.com, sign up a separate account; on a self-managed instance, an admin creates it under **Admin → Users → New user**
    - Add it to each project DAIV should work on with the **Maintainer** role. DAIV only sets up webhooks on projects where its user is a Maintainer or Owner

2. **Navigate to the user's settings**:

    - Sign in to your GitLab instance (e.g., `https://gitlab.com`) as the DAIV user
    - Click your avatar → **Edit profile** → **Access Tokens**

3. **Create New Token**:

    - **Name**: `DAIV Integration`
    - **Expiration**: Set according to your security policy (recommended: 1 year)
    - **Scopes**: Select `api` (full API access)
    - Click **Create personal access token**

4. **Copy the Token**:

    - **Important**: Copy and save the token immediately - you won't see it again
    - The token format looks like: `glpat-xxxxxxxxxxxxxxxxxxxx`

!!! warning "Token Security"
    Store your token securely. Never commit it to version control or share it publicly.

### Step 2: Configure Environment Variables

Add your GitLab token and webhook secret to DAIV's environment configuration.

#### For Docker Compose Setup

Edit your `docker-compose.yml` file:

```yaml
x-app-defaults: &x_app_default
  # ...
  environment:
    CODEBASE_GITLAB_URL: https://gitlab.com # or your GitLab instance URL
    CODEBASE_GITLAB_AUTH_TOKEN: <gitlab-token> # Your personal access token
    CODEBASE_GITLAB_WEBHOOK_SECRET: <webhook-secret> # Random secret for webhook validation
  # ...
```

Replace `<gitlab-token>` with your personal access token and `<webhook-secret>` with a random secret. They are YAML values, so you edit them in place.

#### For Docker Swarm Setup

Create Docker secrets:

```bash
# Create secrets for GitLab integration
echo "<gitlab-token>" | docker secret create codebase_gitlab_auth_token -
echo "<webhook-secret>" | docker secret create codebase_gitlab_webhook_secret -
```

Replace `<gitlab-token>` with your personal access token and `<webhook-secret>` with a random secret.

!!! tip "Generating Webhook Secret"
    Generate a secure random webhook secret:
    ```bash
    openssl rand -hex 32
    ```

### Step 3: Repository Webhooks

DAIV uses webhooks to receive real-time notifications from GitLab about repository events.

DAIV's scheduler adds a webhook to every repository its user maintains, every 5 minutes. No manual setup is required — once DAIV is running with valid GitLab credentials, webhooks are created automatically for new repositories. To add them right away, or to update existing ones after changing `DAIV_EXTERNAL_URL` or the webhook secret, run the command in the app container:

```bash
docker compose exec app django-admin setup_webhooks           # add missing webhooks now
docker compose exec app django-admin setup_webhooks --update  # also update existing ones
```

GitLab sends each event to `DAIV_EXTERNAL_URL`, so that URL must be reachable from your GitLab. A self-managed GitLab refuses webhooks to private network addresses by default (`Url is blocked: Requests to the local network are not allowed`); an admin allows them under **Admin → Settings → Network → Outbound requests**.

!!! tip "Verifying webhook setup"
    To confirm webhooks are configured, go to your GitLab repository → **Settings** → **Webhooks**. You should see a webhook pointing to `https://daiv.example.com/api/codebase/callbacks/gitlab/` with the following trigger events:

    - Push events
    - Issues events
    - Comments (Note events)
    - Merge request events
    - Pipeline events

    Click **Test** → **Push events** to verify connectivity.

### Step 4: Configure Repository Behavior

To customize DAIV's behavior, create a `.daiv.yml` file in your repository's root.

For complete configuration options, see [Repository Config](../customization/repository-config.md).

### Step 5: Test the Integration

Verify that DAIV is properly connected to your repository.

1. **Create a Test Issue**:
    - Go to your GitLab repository
    - Create a new issue with title: "Add hello world function"
    - Add the `daiv` label to the issue. If the project has no `daiv` label yet, create it from the label picker

2. **Wait for DAIV Response**:
    - DAIV reacts with 👀 within seconds, then comments with a plan to address the issue
    - Check the issue comments for DAIV's response

3. **Approve the Plan**:
    - From your own account, comment on the issue mentioning DAIV's user, for example `@daiv proceed`
    - DAIV implements the plan and opens a merge request

!!! tip
    For more on how issue addressing works, see [Issue Addressing](../features/issue-addressing.md).

### Troubleshooting

**Webhook delivery fails**:

- Verify the webhook URL is accessible from GitLab
- Check SSL certificate validity
- Review firewall settings
- On a self-managed GitLab, allow requests to the local network if DAIV has a private address (see [Step 3](#step-3-repository-webhooks))

**Issues not being processed**:

- Ensure the `daiv` label is added to issues
- Check that the project has a webhook: DAIV's user must be a Maintainer or Owner of it
- Verify `issue_addressing.enabled: true` in `.daiv.yml` (enabled by default)
- Check DAIV logs for errors

**DAIV ignores your comments**:

- Comment from an account other than DAIV's own: DAIV ignores what its own user writes
- Mention DAIV's username, for example `@daiv`

**DAIV comments "An unexpected error occurred"**:

- The run failed. The worker and sandbox logs show why, for example `docker compose logs worker sandbox`

**No response to merge request comments**:

- Verify webhook events include "Comments (Note events)"
- Check that webhook secret matches environment variable
- Review repository permissions

---

## GitHub Configuration

### Prerequisites

Before configuring a GitHub repository, ensure you have:

- **DAIV installed and running** - Follow the [Deployment](deployment.md) guide first
- **GitHub repository or organization access** - Admin permissions to create and install GitHub Apps
- **GitHub account** - On GitHub.com or GitHub Enterprise

### Step 1: Create GitHub App

DAIV uses GitHub App authentication to securely interact with your repositories.

1. **Navigate to GitHub App Settings**:

    - For personal account: Go to **Settings** → **Developer settings** → **GitHub Apps** → **New GitHub App**
    - For organization: Go to **Organization Settings** → **Developer settings** → **GitHub Apps** → **New GitHub App**

2. **Configure GitHub App**:

    **Basic Information:**
    - **GitHub App name**: `DAIV` (or `DAIV - YourOrgName`)
    - **Homepage URL**: Your DAIV instance URL (e.g., `https://daiv.example.com`)
    - **Webhook URL**: `https://daiv.example.com/api/codebase/callbacks/github/`
    - **Webhook secret**: Generate a secure random secret (save this for later)
      ```bash
      openssl rand -hex 32
      ```

    **Permissions:**
    - **Repository permissions:**
        - Contents: **Read & Write** (to create branches and commits)
        - Issues: **Read & Write** (to read and comment on issues)
        - Pull requests: **Read & Write** (to create and update pull requests)
        - Actions: **Read-only** (to read workflow runs and job logs for [Pipeline Watch](../features/pipeline-watch.md))
        - Workflows: **Read & Write** (optional; lets DAIV push changes to files under `.github/workflows/`, such as a conflict resolved there. Code running in the sandbox can then change workflows too, and they run with the repository's Actions secrets)
        - Metadata: **Read-only** (automatically selected)
    - **Subscribe to events:** select these events:
        - Push
        - Issues
        - Issue comment
        - Pull request review
        - Pull request review comment (mentions and comments on diff lines of a pull request a DAIV session owns)
        - Pull request
        - Workflow run

    !!! note "Centralized Webhook Configuration"
        Unlike GitLab where webhooks are configured per repository, GitHub Apps use centralized webhook configuration. The webhook you configure here will automatically apply to all repositories where you install the app.

3. **Create the App**:
    - Click **Create GitHub App**
    - You'll be redirected to the app's settings page

4. **Generate Private Key**:
    - Scroll down to **Private keys** section
    - Click **Generate a private key**
    - Save the downloaded `.pem` file securely

5. **Note App ID**:
    - At the top of the app settings page, note the **App ID** (you'll need this)

### Step 2: Install GitHub App

Install the GitHub App on repositories where you want DAIV to work:

1. **Navigate to Install App**:
    - Go to your GitHub App settings page
    - Click **Install App** in the left sidebar
    - Select your account or organization

2. **Choose Repository Access**:
    - Select **All repositories** or **Only select repositories**
    - Choose the repositories you want DAIV to access
    - Click **Install**

3. **Note Installation ID**:
    - After installation, check the URL: `https://github.com/settings/installations/INSTALLATION_ID`
    - Note the **Installation ID** from the URL or use the GitHub API to find it

### Step 3: Configure Environment Variables

Add your GitHub App credentials to DAIV's environment configuration.

#### For Docker Compose Setup

Edit your `docker-compose.yml` file:

```yaml
x-app-defaults: &x_app_default
  # ...
  environment:
    CODEBASE_CLIENT: github
    CODEBASE_GITHUB_APP_ID: 123456 # Your GitHub App ID
    CODEBASE_GITHUB_INSTALLATION_ID: 789012 # Your Installation ID
    CODEBASE_GITHUB_WEBHOOK_SECRET: <webhook-secret> # The secret you generated
    # For GitHub Enterprise, also set:
    # CODEBASE_GITHUB_URL: https://github.example.com
  # ...
```

Replace `<webhook-secret>` with the secret you generated. It is a YAML value, so you edit it in place.

For the private key, you have two options:

**Option 1: Use a Compose secret (recommended)**

Docker Compose mounts a file-based secret at `/run/secrets/<name>`, where DAIV reads it. Add this to your `docker-compose.yml`, pointing `file` at the downloaded `.pem` file:

```yaml
x-app-defaults: &x_app_default
  secrets:
    - codebase_github_private_key

secrets:
  codebase_github_private_key:
    file: <private-key-path>
```

Replace `<private-key-path>` with the path of the downloaded `.pem` file. It is a YAML value, so you edit it in place.

**Option 2: Set as environment variable**

```yaml
x-app-defaults: &x_app_default
  environment:
    CODEBASE_GITHUB_PRIVATE_KEY: |
      -----BEGIN RSA PRIVATE KEY-----
      Your private key content here...
      -----END RSA PRIVATE KEY-----
```

#### For Docker Swarm Setup

Create Docker secrets:

```bash
# Create secrets for GitHub integration
echo "123456" | docker secret create codebase_github_app_id -
echo "789012" | docker secret create codebase_github_installation_id -
docker secret create codebase_github_private_key <private-key-path>
echo "<webhook-secret>" | docker secret create codebase_github_webhook_secret -
```

Replace `<private-key-path>` with the path of the downloaded `.pem` file and `<webhook-secret>` with the secret you generated.

!!! warning "Private Key Format"
    The private key must be in PEM format. Keep the entire key including the header and footer lines (`-----BEGIN RSA PRIVATE KEY-----` and `-----END RSA PRIVATE KEY-----`).

### Step 4: Verify Webhook Configuration

Webhooks are configured at the GitHub App level and automatically apply to all repositories where the app is installed. The webhook configuration you set up in Step 1 should already be active.

#### Verify Webhook Settings

1. **Check Webhook Configuration**:
    - Go to your GitHub App settings page
    - Scroll down to the **Webhook** section
    - Verify the following settings:
        - **Webhook URL**: `https://daiv.example.com/api/codebase/callbacks/github/`
        - **Webhook secret**: The secret you generated earlier
        - **SSL verification**: Enabled (unless using self-signed certificates)
        - **Active**: Checked

2. **Verify Subscribed Events**:
    - Scroll down to **Permissions & events** section
    - Under **Subscribe to events**, ensure these are checked:
        - Push
        - Issues
        - Issue comment
        - Pull request review
        - Pull request review comment (mentions and comments on diff lines of a pull request a DAIV session owns)
        - Pull request
        - Workflow run (only listed once **Actions** is set to **Read-only**, see Step 1)

3. **Test Webhook Delivery**:
    - Create a test issue in one of your repositories where the app is installed
    - Go back to your GitHub App settings
    - Click on **Advanced** → **Recent Deliveries**
    - Check for successful deliveries (200 response code)
    - If you see a delivery, click on it to view the request and response details

!!! tip "Troubleshooting Webhook Delivery"
    If webhook deliveries are failing:

    - Verify the webhook URL is accessible from GitHub
    - Check that SSL certificates are valid (or disable SSL verification for testing)
    - Ensure the webhook secret in GitHub App matches `CODEBASE_GITHUB_WEBHOOK_SECRET`
    - Review DAIV logs for incoming webhook requests

### Step 5: Configure Repository Behavior

To customize DAIV's behavior, create a `.daiv.yml` file in your repository's root.

For complete configuration options, see [Repository Config](../customization/repository-config.md).

### Step 6: Test the Integration

Verify that DAIV is properly connected to your GitHub repository.

1. **Create a Test Issue**:
    - Go to your GitHub repository
    - Create a new issue with title: "Add hello world function"
    - Add the `daiv` label to the issue

2. **Wait for DAIV Response**:
    - DAIV should automatically comment with a plan to address the issue
    - Check the issue comments for DAIV's response

!!! tip
    For more on how issue addressing works, see [Issue Addressing](../features/issue-addressing.md).

### Troubleshooting

**Issues not being processed**:

- Ensure the `daiv` label exists and is added to issues
- Verify the GitHub App has proper permissions (Issues: Read & Write)
- Check DAIV logs for errors
- Verify `issue_addressing.enabled: true` in `.daiv.yml` (enabled by default)

**No response to comments**:

- Verify webhook events include "Issue comments" and "Pull request reviews"
- Check that webhook secret matches environment variable
- Ensure the app is installed on the repository

**Authentication errors**:

- Verify the private key format is correct (PEM format with header/footer)
- Check App ID and Installation ID are correct
- Ensure the GitHub App is installed on the target repositories

---

## Next steps

With your repository connected, you can now:

- **[LLM Providers](llm-providers.md)** — configure your LLM provider and API keys
- **[Repository Config](../customization/repository-config.md)** — customize DAIV's behavior per repository
- **[Issue Addressing](../features/issue-addressing.md)** — learn how DAIV handles issues
- **[Pull Request Assistant](../features/pull-request-assistant.md)** — learn how DAIV reviews code
