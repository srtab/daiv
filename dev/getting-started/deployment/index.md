# Deployment

This guide walks you through deploying DAIV using Docker Swarm or Docker Compose. After completing this guide, you'll have a fully functional DAIV instance ready to connect to your codebase.

## What You'll Deploy

**DAIV requires several core services to function properly**. You'll deploy these services using container orchestration:

**Required services:**

- **[PostgreSQL](https://www.postgresql.org/)** — stores application data
- **[Redis](https://redis.io/)** — handles caching
- **[DAIV Application](https://github.com/srtab/daiv)** — main API
- **[DAIV Worker](https://docs.djangoproject.com/en/6.0/topics/tasks/)** — background task processor

**Optional services:**

- **[DAIV Scheduler](https://pypi.org/project/django-crontask/)** — periodic task scheduler
- **[DAIV Sandbox](https://github.com/srtab/daiv-sandbox)** — isolated environment for running commands (see [Sandbox](https://srtab.github.io/daiv/dev/features/sandbox/index.md))
- **MCP Servers** — isolated containers for MCP tools via [supergateway](https://github.com/supercorp-ai/supergateway) (see [MCP Tools](https://srtab.github.io/daiv/dev/customization/mcp-tools/index.md))

______________________________________________________________________

## Docker Swarm (*Recommended*)

**Docker Swarm provides better production deployment capabilities** including service discovery, load balancing, and rolling updates. This guide covers single-server deployment, but you can extend it to multiple servers using the [Docker Swarm documentation](https://docs.docker.com/engine/swarm/swarm-tutorial/).

**Prerequisites**

- [Docker installed](https://docs.docker.com/engine/install/) with [Swarm enabled](https://docs.docker.com/engine/swarm/swarm-tutorial/)
- Internet connection to pull container images
- Basic understanding of Docker Swarm

### Step 1: Create Docker Secrets

**Before deploying, you must create these Docker secrets**. These secrets store sensitive configuration data securely:

**Required Secrets:**

- **`django_secret_key`** - Random secret key for Django ([generate one here](https://djecrety.ir/))
- **`db_password`** - Random password for the PostgreSQL database
- **`codebase_gitlab_auth_token`** - GitLab personal access token with `api` scope (see [Platform Setup](https://srtab.github.io/daiv/dev/getting-started/platform-setup/#gitlab-configuration))
- **`codebase_gitlab_webhook_secret`** - Random secret for GitLab webhook validation
- **`daiv_sandbox_api_key`** - Random API key for Sandbox service authentication
- **`openrouter_api_key`** - [OpenRouter API key](https://openrouter.ai/settings/keys) for LLM access
- **`allauth_github_client_id`** - GitHub OAuth App client ID (see [Authentication](https://srtab.github.io/daiv/dev/reference/env-variables/#authentication))
- **`allauth_github_secret`** - GitHub OAuth App secret
- **`allauth_gitlab_client_id`** - GitLab OAuth Application ID (see [Authentication](https://srtab.github.io/daiv/dev/reference/env-variables/#authentication))
- **`allauth_gitlab_secret`** - GitLab OAuth Application secret

**Create each secret using this command** (see [Docker Secrets documentation](https://docs.docker.com/reference/cli/docker/secret/create/) for more details):

```
docker secret create django_secret_key <secret_key>
```

Additional Secrets May Be Required

These are the minimal secrets for basic DAIV functionality. Check the [Environment Variables](https://srtab.github.io/daiv/dev/reference/env-variables/index.md) page for additional secrets needed for specific features or services.

### Step 2: Create `stack.yml` file

**Create your deployment configuration file**. This YAML file defines all services, networks, and volumes needed for DAIV.

Customize Environment Variables

**Replace all annotated values with your own configuration**. See the [Environment Variables](https://srtab.github.io/daiv/dev/reference/env-variables/index.md) page for complete configuration options.

```
x-app-environment-defaults: &app_environment_defaults
  # DJANGO
  DJANGO_SETTINGS_MODULE: daiv.settings.production
  DJANGO_ALLOWED_HOSTS: your-hostname.com,app,127.0.0.1 (1)
  DJANGO_REDIS_URL: redis://daiv_redis:6379/0
  DJANGO_REDIS_SESSION_URL: redis://daiv_redis:6379/1
  DJANGO_REDIS_CHECKPOINT_URL: redis://daiv_redis:6379/2
  DAIV_EXTERNAL_URL: https://your-hostname.com (2)
  # DATABASE
  DB_NAME: daiv
  DB_USER: daiv_admin
  DB_HOST: daiv_db
  DB_SSLMODE: prefer
  # CODEBASE
  CODEBASE_CLIENT: gitlab
  CODEBASE_GITLAB_URL: https://gitlab.com (3)
  # SANDBOX
  DAIV_SANDBOX_URL: http://sandbox:8000 (4)

x-deploy-defaults: &deploy_defaults
  replicas: 1
  update_config:
    order: start-first
    delay: 60s
    failure_action: rollback
  rollback_config:
    parallelism: 0
  restart_policy:
    condition: any
    window: 120s

services:
  db:
    image: postgres:17.6
    environment:
      - POSTGRES_DB=daiv
      - POSTGRES_USER=daiv_admin
      - POSTGRES_PASSWORD_FILE=/run/secrets/db_password
    networks:
      - internal
    secrets:
      - db_password
    volumes:
      - db-volume:/var/lib/postgresql/data
    stop_grace_period: 30s
    healthcheck:
      test: pg_isready -q -d $$POSTGRES_DB -U $$POSTGRES_USER
      interval: 10s
      start_period: 120s
    deploy:
      replicas: 1
      update_config:
        failure_action: rollback
        delay: 10s
      rollback_config:
        parallelism: 0
      restart_policy:
        condition: any
        window: 120s

  redis:
    image: redis:7-alpine
    networks:
      - internal
    volumes:
      - redis-volume:/data
    healthcheck:
      test: redis-cli ping || exit 1
      interval: 10s
      start_period: 30s
    deploy:
      <<: *deploy_defaults

  app:
    image: ghcr.io/srtab/daiv:latest (5)
    environment:
      <<: *app_environment_defaults
    secrets:
      - django_secret_key
      - db_password
      - codebase_gitlab_auth_token
      - codebase_gitlab_webhook_secret
      - daiv_sandbox_api_key
      - openrouter_api_key
      - allauth_github_client_id
      - allauth_github_secret
      - allauth_gitlab_client_id
      - allauth_gitlab_secret
    networks:
      - internal
      - external
    ports:
      - "8000:8000"
    deploy:
      <<: *deploy_defaults

  worker:
    image: ghcr.io/srtab/daiv:latest (5)
    command: sh /home/daiv/start-worker
    environment:
      <<: *app_environment_defaults
    secrets:
      - django_secret_key
      - db_password
      - codebase_gitlab_auth_token
      - codebase_gitlab_webhook_secret
      - daiv_sandbox_api_key
      - openrouter_api_key
    networks:
      - internal
    # volumes:  (16)
    #   - ./custom-skills:/home/daiv/data/skills:ro
    healthcheck:
      test: grep -q 'db_worker' /proc/*/cmdline 2>/dev/null
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    deploy:
      <<: *deploy_defaults
      replicas: 1 (10)

  scheduler:
    image: ghcr.io/srtab/daiv:latest (5)
    command: sh /home/daiv/start-crontask
    environment:
      <<: *app_environment_defaults
    secrets:
      - django_secret_key
      - db_password
      - codebase_gitlab_auth_token
      - codebase_gitlab_webhook_secret
      - daiv_sandbox_api_key
      - openrouter_api_key
    networks:
      - internal
    healthcheck:
      test: grep -q 'crontask' /proc/*/cmdline 2>/dev/null
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    deploy:
      <<: *deploy_defaults

  sandbox:
    image: ghcr.io/srtab/daiv-sandbox:latest (5)
    environment:
      DAIV_SANDBOX_KEEP_TEMPLATE: true (6)
    networks:
      - internal
    secrets:
      - daiv_sandbox_api_key
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock (7)
      - $HOME/.docker/config.json:/home/app/.docker/config.json (8)
    deploy:
      <<: *deploy_defaults

  mcp-sentry:
    image: supercorp/supergateway:latest
    command:
      - --stdio
      - "npx @sentry/mcp-server@latest --access-token=$$(cat /run/secrets/sentry_access_token)" (9)
      - --healthEndpoint
      - "/healthz"
    environment:
      SENTRY_HOST: your-sentry-host
    secrets:
      - sentry_access_token
    networks:
      - internal
    healthcheck:
      test: wget --spider -q http://localhost:8000/healthz || exit 1
      interval: 30s
      start_period: 30s
    deploy:
      <<: *deploy_defaults

  mcp-context7:
    image: supercorp/supergateway:latest
    command:
      - --stdio
      - "npx @upstash/context7-mcp@latest"
      - --healthEndpoint
      - "/healthz"
    networks:
      - internal
    healthcheck:
      test: wget --spider -q http://localhost:8000/healthz || exit 1
      interval: 30s
      start_period: 30s
    deploy:
      <<: *deploy_defaults


networks:
  internal:
    driver: overlay
  external:
    driver: overlay

volumes:
  db-volume:
    driver: local
  redis-volume:
    driver: local

secrets:
  django_secret_key:
    external: true
  db_password:
    external: true
  codebase_gitlab_auth_token:
    external: true
  codebase_gitlab_webhook_secret:
    external: true
  daiv_sandbox_api_key:
    external: true
  openrouter_api_key:
    external: true
  sentry_access_token:
    external: true
  allauth_github_client_id:
    external: true
  allauth_github_secret:
    external: true
  allauth_gitlab_client_id:
    external: true
  allauth_gitlab_secret:
    external: true
```

1. Replace `your-hostname.com` with your domain name. Don't include the schema (e.g., use `daiv.com` not `https://daiv.com`). Keep `app` and `127.0.0.1` for internal service communication.
1. Replace with your full domain URL including schema (e.g., `https://your-hostname.com`)
1. Set to your GitLab instance URL (e.g., `https://gitlab.com` for GitLab.com)
1. Points to the Sandbox service. Use `http://sandbox:8000` when deploying Sandbox in the same stack
1. **Recommended**: Replace `latest` with a specific version tag for production deployments
1. See [DAIV Sandbox documentation](https://github.com/srtab/daiv-sandbox) for configuration details
1. **Required**: Sandbox needs Docker socket access to create isolated containers
1. **Optional**: Remove this volume if you don't need private registry access
1. The Sentry access token is read from the Docker secret at runtime via `--access-token`. Set `SENTRY_HOST` for self-hosted Sentry instances. These MCP services are optional — remove them if not needed
1. **Scaling**: Increase `replicas` to handle more concurrent tasks (e.g., `replicas: 3`). Each worker processes tasks independently from the shared queue, so adding replicas scales DAIV's throughput with no architecture changes
1. **Optional**: Uncomment to mount [custom global skills](https://srtab.github.io/daiv/dev/customization/agent-skills/#custom-global-skills) that are available across all repositories

### Step 3: Deploy the stack

**Deploy your DAIV stack** by running this command from the directory containing your `stack.yml` file:

```
docker stack deploy -c stack.yml daiv
```

**Monitor deployment progress** with these commands:

```
# Check service status with full details
docker stack ps daiv --no-trunc

# Or check running containers
docker ps
```

Deployment Time

**Services may take several minutes to become fully healthy**, especially during the initial deployment when images are being pulled and databases are being initialized.

### Step 4: Next steps

Your DAIV deployment is running. Follow the [Reverse Proxy](#reverse-proxy) guide below to configure external access, then proceed to [Platform Setup](https://srtab.github.io/daiv/dev/getting-started/platform-setup/index.md) to connect your first repository.

______________________________________________________________________

## Docker Compose

**Docker Compose provides simpler deployment** suitable for development environments or smaller production setups. This method uses a single configuration file to manage all services.

**Prerequisites**

- [Docker installed](https://docs.docker.com/engine/install/) with [Compose](https://docs.docker.com/compose/install/)
- Internet connection to pull container images

### Step 1: Create `docker-compose.yml` file

**Create your Docker Compose configuration**. This file defines all services and their configurations in a single place.

Environment Variable Configuration

**Replace all annotated values with your specific configuration**. See the [Environment Variables](https://srtab.github.io/daiv/dev/reference/env-variables/index.md) page for additional options.

```
x-app-defaults: &x_app_default
  image: ghcr.io/srtab/daiv:latest
  restart: unless-stopped
  environment:
    DJANGO_SETTINGS_MODULE: daiv.settings.production
    DJANGO_SECRET_KEY: secret-key (1)
    DJANGO_ALLOWED_HOSTS: your-hostname.com,app,127.0.0.1 (2)
    DJANGO_REDIS_URL: redis://redis:6379/0
    DJANGO_REDIS_SESSION_URL: redis://redis:6379/1
    DJANGO_REDIS_CHECKPOINT_URL: redis://redis:6379/2
    DAIV_EXTERNAL_URL: https://your-hostname.com (12)
    # Database settings
    DB_HOST: db
    DB_NAME: daiv
    DB_USER: daiv
    DB_PASSWORD: daivpass (3)
    DB_SSLMODE: prefer
    # Codebase settings
    CODEBASE_CLIENT: gitlab
    CODEBASE_GITLAB_URL: https://gitlab.com (4)
    CODEBASE_GITLAB_AUTH_TOKEN: gitlab-auth-token (5)
    CODEBASE_GITLAB_WEBHOOK_SECRET: gitlab-webhook-secret (6)
    # LLM Providers settings
    OPENROUTER_API_KEY: openrouter-api-key (8)
    # Sandbox settings
    DAIV_SANDBOX_API_KEY: daiv-sandbox-api-key (9)
    # Authentication (at least one social provider recommended)
    ALLAUTH_GITHUB_CLIENT_ID: github-client-id
    ALLAUTH_GITHUB_SECRET: github-secret
    ALLAUTH_GITLAB_CLIENT_ID: gitlab-client-id
    ALLAUTH_GITLAB_SECRET: gitlab-secret

services:
  db:
    image: postgres:17.6
    container_name: daiv-db
    restart: unless-stopped
    environment:
      POSTGRES_DB: daiv
      POSTGRES_USER: daiv
      POSTGRES_PASSWORD: daivpass (10)
    volumes:
      - db-volume:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U daiv -d daiv"]
      interval: 10s
      timeout: 10s
      start_period: 30s
      retries: 5

  redis:
    image: redis:latest
    restart: unless-stopped
    container_name: daiv-redis
    volumes:
      - redis-volume:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

  app:
    <<: *x_app_default
    container_name: daiv-app
    ports:
      - "8000:8000"
    depends_on:
      db:
        condition: service_healthy
        restart: true
      redis:
        condition: service_healthy
        restart: true
      sandbox:
        condition: service_healthy

  worker:
    <<: *x_app_default
    command: sh /home/daiv/start-worker
    # volumes:  (17)
    #   - ./custom-skills:/home/daiv/data/skills:ro
    healthcheck:
      test: ["CMD-SHELL", "grep -q 'db_worker' /proc/*/cmdline 2>/dev/null"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    ports: []
    deploy:
      replicas: 1 (15)
    depends_on:
      app:
        condition: service_healthy
        restart: true

  scheduler:
    <<: *x_app_default
    container_name: daiv-scheduler
    command: sh /home/daiv/start-crontask
    healthcheck:
      test: ["CMD-SHELL", "grep -q 'crontask' /proc/*/cmdline 2>/dev/null"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    ports: []
    depends_on:
      app:
        condition: service_healthy
        restart: true

  sandbox:
    image: ghcr.io/srtab/daiv-sandbox:latest
    restart: unless-stopped
    container_name: daiv-sandbox
    group_add:
      - 987 (13)
    environment:
      DAIV_SANDBOX_API_KEY: daiv-sandbox-api-key (11)
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock

  mcp-sentry:
    image: supercorp/supergateway:latest
    restart: unless-stopped
    container_name: daiv-mcp-sentry
    command:
      - --stdio
      - "npx @sentry/mcp-server@latest"
      - --healthEndpoint
      - "/healthz"
    env_file:
      - config.secrets.env (14)
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://localhost:8000/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s

  mcp-context7:
    image: supercorp/supergateway:latest
    restart: unless-stopped
    container_name: daiv-mcp-context7
    command:
      - --stdio
      - "npx @upstash/context7-mcp@latest"
      - --healthEndpoint
      - "/healthz"
    env_file:
      - config.secrets.env
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://localhost:8000/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s

volumes:
  db-volume:
    driver: local
  redis-volume:
    driver: local
```

1. **[Generate a Django secret key](https://djecrety.ir/)** - Use a cryptographically secure random string
1. **Replace with your domain name** - Don't include schema (e.g., `daiv.com`)
1. **Generate a secure random password** for the database
1. **Set your GitLab instance URL** (e.g., `https://gitlab.com`)
1. **Create a GitLab personal access token** with `api` scope permissions (see [Platform Setup](https://srtab.github.io/daiv/dev/getting-started/platform-setup/#gitlab-configuration))
1. **Generate a random webhook secret** for GitLab webhook validation
1. **Get an OpenRouter API key** for LLM model access
1. **Generate a random API key** for Sandbox service authentication
1. **Use the same password** as defined in annotation 3
1. **Use the same API key** as defined in annotation 9
1. **Include the full URL with schema** (e.g., `https://your-hostname.com`)
1. **Add the docker group** to the sandbox container (`stat -c '%g' /var/run/docker.sock`)
1. **Add MCP credentials** (`SENTRY_ACCESS_TOKEN`, `CONTEXT7_API_KEY`) to your env file. These MCP services are optional — remove them if not needed
1. **Scaling**: Increase `replicas` to handle more concurrent tasks (e.g., `replicas: 3`). Each worker processes tasks independently from the shared queue, so adding replicas scales DAIV's throughput with no architecture changes
1. **Optional**: Uncomment to mount [custom global skills](https://srtab.github.io/daiv/dev/customization/agent-skills/#custom-global-skills) that are available across all repositories

### Step 2: Run the compose file

**Start all DAIV services** by running this command from the directory containing your `docker-compose.yml`:

```
docker compose up -d
```

**Check service status** to ensure everything is running correctly:

```
docker compose ps
```

### Step 3: Next steps

Your DAIV instance is running. Continue with the [Reverse Proxy](#reverse-proxy) configuration below, then proceed to [Platform Setup](https://srtab.github.io/daiv/dev/getting-started/platform-setup/index.md) to connect your first repository.

______________________________________________________________________

## Reverse Proxy

**Configure a reverse proxy** to provide secure external access to your DAIV instance. This setup enables HTTPS access and proper domain routing.

**This guide covers Nginx configuration**. Basic Nginx knowledge is assumed.

Contributions Welcome

**Only Nginx configuration is provided currently**. Contributions for Apache, Traefik, and other reverse proxy configurations are welcome!

**Prerequisites**

- [Nginx installed](https://docs.nginx.com/nginx/admin-guide/installing-nginx/installing-nginx-open-source/)
- Valid SSL certificate for your domain
- Domain name pointing to your server

### Step 1: Configure Nginx

**Create a new Nginx configuration file** at `/etc/nginx/conf.d/daiv.conf` (path may vary by operating system).

**Add this configuration and customize the annotated values**:

```
upstream daiv-instance {
  server internal-ip:8000;  (1)
}

server {
  listen              443 ssl;
  listen              [::]:443 ssl;

  http2               on;

  server_name         your-hostname.com;  (2)

  # SSL Configuration.
  # You can use this https://ssl-config.mozilla.org/ to generate
  # the correct ssl configuration for your server.
  ssl_certificate      /etc/pki/tls/certs/ssl.crt;  (3)
  ssl_certificate_key  /etc/pki/tls/private/ssl.key;  (4)

  ssl_protocols TLSv1.3;
  ssl_ecdh_curve X25519:prime256v1:secp384r1;
  ssl_prefer_server_ciphers off;

  location / {
    proxy_pass              http://daiv-instance;
    proxy_set_header        Host $host;
    proxy_set_header        X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header        X-Forwarded-Proto $scheme;
    proxy_set_header        X-Real-IP $remote_addr;
    proxy_redirect          off;
    proxy_buffering         off;
    proxy_connect_timeout   60;
    proxy_send_timeout      60;
    proxy_read_timeout      60;

    add_header Strict-Transport-Security "max-age=63072000" always;
  }
}

server {
    listen 80 default_server;
    listen [::]:80;

    return 301 https://$host$request_uri;
}
```

1. **Set the internal IP** of your DAIV instance. Use `localhost` or `127.0.0.1` if running on the same server
1. **Replace with your domain name** (e.g., `daiv.example.com`)
1. **Update the SSL certificate path** - Location varies by operating system
1. **Update the SSL certificate key path** - Location varies by operating system

### Step 2: Restart Nginx

**Apply the configuration changes** by restarting Nginx:

```
systemctl restart nginx
```

**Verify the configuration** by accessing your domain in a web browser. You should see the DAIV login page at `https://your-domain/accounts/login/`.

______________________________________________________________________

## Next steps

Your DAIV instance is now running and accessible. Continue with:

1. **[Platform Setup](https://srtab.github.io/daiv/dev/getting-started/platform-setup/index.md)** — connect DAIV to your GitLab or GitHub repositories
1. **[LLM Providers](https://srtab.github.io/daiv/dev/getting-started/llm-providers/index.md)** — configure your LLM provider and API keys
1. **[Repository Config](https://srtab.github.io/daiv/dev/customization/repository-config/index.md)** — customize DAIV's behavior per repository
