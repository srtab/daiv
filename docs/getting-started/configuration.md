# Repository Configuration

This guide walks you through connecting DAIV to your GitLab repository. Once configured, DAIV will automatically respond to issues, pull requests, and pipeline failures.

---

## Prerequisites

Before configuring a repository, ensure you have:

- **DAIV installed and running** - Follow the [installation guide](up-and-running.md) first
- **GitLab repository access** - Admin or maintainer permissions on the repository you want to connect
- **GitLab personal access token** - With `api` scope permissions

---

## Step 1: Create GitLab Personal Access Token

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

---

## Step 2: Configure Environment Variables

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

---

## Step 3: Set Up Repository Webhooks

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

---

## Step 4: Index Repository Content

DAIV needs to index your repository content to provide context-aware assistance.

```bash
# Enter the DAIV container
docker compose exec -it app bash

# Index all accessible repositories
django-admin update_index

# Index a specific repository
django-admin update_index --repo-id "group/repository-name"
```

The indexing process will:
- Clone the repository content
- Extract and chunk code files
- Generate embeddings for semantic search
- Build search indices for fast retrieval

!!! info "Indexing Time"
    Initial indexing may take several seconds depending on repository size. Subsequent updates are incremental and faster.

---

## Step 5: Configure Repository Behavior

Create a `.daiv.yml` file in your repository's root to customize DAIV's behavior.

For complete configuration options, see [Repository Configurations](repository-configurations.md).

---

## Step 6: Test the Integration

Verify that DAIV is properly connected to your repository.

1. **Create a Test Issue**:
    - Go to your GitLab repository
    - Create a new issue with title: "Add hello world function"
    - Add the `daiv` label to the issue

2. **Wait for DAIV Response**:
    - DAIV should automatically comment with a plan to address the issue
    - Check the issue comments for DAIV's response

---

## Troubleshooting

### Common Issues

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

## Next Steps

With your repository configured, you can now:

- **[Learn about AI agents](../ai-agents/overview.md)** - Understand how DAIV's agents work
- **[Customize agent behavior](repository-configurations.md)** - Fine-tune DAIV for your workflow
- **[Configure monitoring](monitoring.md)** - Configure LangSmith for monitoring
