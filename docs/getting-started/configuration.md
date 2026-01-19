# Repository Configuration

This guide walks you through connecting DAIV to your GitLab or GitHub repository. Once configured, DAIV will automatically implement the issues, respond to code reviews, and more...

---

## Choose Your Platform

DAIV supports both GitLab and GitHub. Choose the platform you want to configure:

- **[GitLab Configuration](#gitlab-configuration)** - For GitLab.com or self-hosted GitLab instances
- **[GitHub Configuration](#github-configuration)** - For GitHub.com or GitHub Enterprise

---

## GitLab Configuration

---

### Prerequisites

Before configuring a GitLab repository, ensure you have:

- **DAIV installed and running** - Follow the [installation guide](up-and-running.md) first
- **GitLab repository access** - Admin or maintainer permissions on the repository you want to connect
- **GitLab personal access token** - With `api` scope permissions

### Step 1: Create GitLab Personal Access Token

DAIV needs a GitLab personal access token to interact with your repositories.

1. **Navigate to GitLab Settings**:

    - Go to your GitLab instance (e.g., `https://gitlab.com`)
    - Click your avatar → **Edit profile** → **Access Tokens**

2. **Create New Token**:

    - **Name**: `DAIV Integration`
    - **Expiration**: Set according to your security policy (recommended: 1 year)
    - **Scopes**: Select `api` (full API access)
    - Click **Create personal access token**

3. **Copy the Token**:

    - **Important**: Copy and save the token immediately - you won't see it again
    - The token format looks like: `glpat-xxxxxxxxxxxxxxxxxxxx`

!!! warning "Token Security"
    Store your token securely. Never commit it to version control or share it publicly.

### Step 2: Configure Environment Variables

Add your GitLab token and webhook secret to DAIV's environment configuration.

### For Docker Compose Setup

Edit your `docker-compose.yml` file:

```yaml
x-app-defaults: &x_app_default
  # ...
  environment:
    CODEBASE_GITLAB_URL: https://gitlab.com # or your GitLab instance URL
    CODEBASE_GITLAB_AUTH_TOKEN: glpat-xxxxxxxxxxxxxxxxxxxx # Your personal access token
    CODEBASE_GITLAB_WEBHOOK_SECRET: your-webhook-secret-here # Random secret for webhook validation
  # ...
```

### For Docker Swarm Setup

Create Docker secrets:

```bash
# Create secrets for GitLab integration
echo "glpat-xxxxxxxxxxxxxxxxxxxx" | docker secret create codebase_gitlab_auth_token -
echo "your-webhook-secret-here" | docker secret create codebase_gitlab_webhook_secret -
```

!!! tip "Generating Webhook Secret"
    Generate a secure random webhook secret:
    ```bash
    openssl rand -hex 32
    ```

### Step 3: Set Up Repository Webhooks

DAIV uses webhooks to receive real-time notifications from GitLab about repository events.

### Automatic Webhook Setup (Recommended)

Use DAIV's management command to automatically set up webhooks for all accessible repositories:

```bash
# Enter the DAIV container
docker compose exec -it app bash

# Set up webhooks for all repositories
django-admin setup_webhooks --base-url https://your-daiv-instance.com

# For local development with self-signed certificates
django-admin setup_webhooks --base-url https://your-daiv-instance.com --disable-ssl-verification
```

### Manual Webhook Setup

If you prefer to set up webhooks manually or for specific repositories:

1. **Navigate to Repository Settings**:
    - Go to your GitLab repository
    - Navigate to **Settings** → **Webhooks**

2. **Add New Webhook**:
    - **URL**: `https://your-daiv-instance.com/api/codebase/callbacks/gitlab/`
    - **Secret token**: Use the same secret from your environment variables
    - **Trigger events**: Select:
        - ✅ Push events
        - ✅ Issues events
        - ✅ Comments (Note events)
        - ✅ Pipeline events
    - **SSL verification**: Enable (unless using self-signed certificates)

3. **Test the Webhook**:
    - Click **Add webhook**
    - Click **Test** → **Push events** to verify connectivity

### Step 4: Configure Repository Behavior

To customize DAIV's behavior, create a `.daiv.yml` file in your repository's root.

For complete configuration options, see [Repository Configurations](../configuration/yaml-config.md).

### Step 5: Test the Integration

Verify that DAIV is properly connected to your repository.

1. **Create a Test Issue**:
    - Go to your GitLab repository
    - Create a new issue with title: "Add hello world function" (or any other issue you want to address)
    - Add the `daiv` label to the issue or start the issue title with "DAIV:"

2. **Wait for DAIV Response**:
    - DAIV should automatically comment with a plan to address the issue
    - Check the issue comments for DAIV's response

### Troubleshooting

**Common Issues**

**Webhook delivery fails**:

- Verify the webhook URL is accessible from GitLab
- Check SSL certificate validity
- Review firewall settings

**Issues not being processed**:

- Ensure the `daiv` label is added to issues
- Verify `auto_address_issues_enabled: true` in `.daiv.yml`
- Check DAIV logs for errors

**No response to comments**:

- Verify webhook events include "Comments"
- Check that webhook secret matches environment variable
- Review repository permissions

---

## GitHub Configuration

### Prerequisites

Before configuring a GitHub repository, ensure you have:

- **DAIV installed and running** - Follow the [installation guide](up-and-running.md) first
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
    - **Webhook URL**: `https://your-daiv-instance.com/api/codebase/callbacks/github/`
    - **Webhook secret**: Generate a secure random secret (save this for later)
      ```bash
      openssl rand -hex 32
      ```

    **Permissions:**
    - **Repository permissions:**
        - Contents: **Read & Write** (to create branches and commits)
        - Issues: **Read & Write** (to read and comment on issues)
        - Pull requests: **Read & Write** (to create and update pull requests)
        - Metadata: **Read-only** (automatically selected)
    - **Subscribe to events:**
        - ✅ Push
        - ✅ Issues
        - ✅ Issue comment
        - ✅ Pull request review

    !!! info "Centralized Webhook Configuration"
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
    CODEBASE_GITHUB_WEBHOOK_SECRET: your-webhook-secret-here # The secret you generated
    # For GitHub Enterprise, also set:
    # CODEBASE_GITHUB_URL: https://github.your-company.com
  # ...
```

For the private key, you have two options:

**Option 1: Use Docker secrets (recommended)**

```bash
# Create secret from the downloaded .pem file
docker secret create codebase_github_private_key /path/to/your-private-key.pem
```

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
docker secret create codebase_github_private_key /path/to/your-private-key.pem
echo "your-webhook-secret-here" | docker secret create codebase_github_webhook_secret -
```

!!! warning "Private Key Format"
    The private key must be in PEM format. Keep the entire key including the header and footer lines (`-----BEGIN RSA PRIVATE KEY-----` and `-----END RSA PRIVATE KEY-----`).

### Step 4: Verify Webhook Configuration

Webhooks are configured at the GitHub App level and automatically apply to all repositories where the app is installed. The webhook configuration you set up in Step 1 should already be active.

#### Verify Webhook Settings

1. **Check Webhook Configuration**:
    - Go to your GitHub App settings page
    - Scroll down to the **Webhook** section
    - Verify the following settings:
        - **Webhook URL**: `https://your-daiv-instance.com/api/codebase/callbacks/github/`
        - **Webhook secret**: The secret you generated earlier
        - **SSL verification**: Enabled (unless using self-signed certificates)
        - **Active**: Checked

2. **Verify Subscribed Events**:
    - Scroll down to **Permissions & events** section
    - Under **Subscribe to events**, ensure these are checked:
        - ✅ Push
        - ✅ Issues
        - ✅ Issue comment
        - ✅ Pull request review

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

For complete configuration options, see [Repository Configurations](../configuration/yaml-config.md).

### Step 6: Test the Integration

Verify that DAIV is properly connected to your GitHub repository.

1. **Create a Test Issue**:
    - Go to your GitHub repository
    - Create a new issue with title: "Add hello world function"
    - Add the `daiv` label to the issue or start with "DAIV:" in the issue title

2. **Wait for DAIV Response**:
    - DAIV should automatically comment with a plan to address the issue
    - Check the issue comments for DAIV's response

3. **Approve the Plan**:
    - Reply to the plan comment with `@daiv proceed` to approve and execute the plan
    - DAIV will create a pull request with the implementation

### Troubleshooting

**Common Issues**

**Issues not being processed**:

- Ensure the `daiv` label exists and is added to issues
- Verify the GitHub App has proper permissions (Issues: Read & Write)
- Check DAIV logs for errors
- Ensure `issue_addressing.enabled: true` in `.daiv.yml`

**No response to comments**:

- Verify webhook events include "Issue comments" and "Pull request reviews"
- Check that webhook secret matches environment variable
- Review GitHub App installation permissions
- Ensure the app is installed on the repository

**Authentication errors**:

- Verify the private key format is correct (including header/footer)
- Check App ID and Installation ID are correct
- Ensure the GitHub App is installed on the target repositories

---

## ⏭️ Next Steps

With your repository configured, you can now:

- **[Learn about AI agents](../ai-agents/overview.md)** - Understand how DAIV's agents work
- **[Customize agent behavior](../configuration/yaml-config.md)** - Fine-tune DAIV for your workflow
- **[Configure monitoring](../configuration/monitoring.md)** - Configure LangSmith for monitoring
