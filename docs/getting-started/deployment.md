---
description: "Deploy a self-hosted DAIV instance with Docker Swarm or Docker Compose: PostgreSQL, Redis, the DAIV app and worker, plus the optional scheduler and sandbox."
---

# Deployment

This guide walks you through deploying DAIV using Docker Swarm or Docker Compose. After completing this guide, you'll have a fully functional DAIV instance ready to connect to your codebase.

## What You'll Deploy

**DAIV requires several core services to function properly**. You'll deploy these services using container orchestration:

* **[PostgreSQL](https://www.postgresql.org/)** — stores application data
* **[Redis](https://redis.io/) 8 or later** — caching, sessions and the agent's checkpoints, which use the search commands Redis 7 lacks
* **[DAIV Application](https://github.com/srtab/daiv)** — web dashboard and API
* **[DAIV Worker](https://docs.djangoproject.com/en/6.0/topics/tasks/)** — background task processor
* **[DAIV Scheduler](https://pypi.org/project/django-crontask/)** — periodic tasks, among them adding webhooks to your GitLab projects and syncing repository access
* **[DAIV Sandbox](https://github.com/srtab/daiv-sandbox)** — isolated containers where the agent runs commands and git, through an egress proxy that holds the git credentials (see [Sandbox](../features/sandbox.md))

!!! tip "Trying DAIV out?"
    The [Quickstart](quickstart.md) runs the smallest working setup on one machine and gets you to a first merge request in about 15 minutes.

---

## :simple-swarm: Docker Swarm (*Recommended*)

**Docker Swarm provides better production deployment capabilities** including service discovery, load balancing, and rolling updates. This guide covers single-server deployment, but you can extend it to multiple servers using the [Docker Swarm documentation](https://docs.docker.com/engine/swarm/swarm-tutorial/).

**Prerequisites**

 * [Docker installed](https://docs.docker.com/engine/install/) with [Swarm enabled](https://docs.docker.com/engine/swarm/swarm-tutorial/)
 * Internet connection to pull container images
 * Basic understanding of Docker Swarm

### Step 1: Create Docker Secrets

**Before deploying, you must create these Docker secrets**. These secrets store sensitive configuration data securely:

**Required Secrets:**

* **`django_secret_key`** - Random secret key for Django ([generate one here](https://djecrety.ir/))
* **`db_password`** - Random password for the PostgreSQL database
* **`codebase_gitlab_auth_token`** - GitLab personal access token with `api` scope (see [Platform Setup](platform-setup.md#gitlab-configuration))
* **`codebase_gitlab_webhook_secret`** - Random secret for GitLab webhook validation
* **`daiv_sandbox_api_key`** - Random API key for Sandbox service authentication
* **`email_host_password`** - SMTP authentication password (if your relay requires authentication)
* **`openrouter_api_key`** - [OpenRouter API key](https://openrouter.ai/settings/keys) for LLM access
* **`allauth_client_id`** - OAuth client ID for the configured Git platform (see [Authentication](../reference/env-variables.md#authentication))
* **`allauth_client_secret`** - OAuth client secret for the configured Git platform
* **`daiv_sandbox_egress_ca_cert`** and **`daiv_sandbox_egress_ca_key`** - the certificate authority the sandbox's egress proxy signs with, created below

**Create each secret using this command** (see [Docker Secrets documentation](https://docs.docker.com/reference/cli/docker/secret/create/) for more details):

```bash
printf '%s' '<value>' | docker secret create <name> -
```

**Create the egress CA and store it as two secrets.** DAIV runs git inside the sandbox, and the sandbox reaches your Git platform only through a per-session proxy that adds the credentials, so the token never enters the sandbox. The proxy needs this CA:

```bash
openssl req -x509 -newkey rsa:4096 -nodes -days 3650 -subj "/CN=daiv-sandbox-egress" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout egress-ca.key -out egress-ca.crt
docker secret create daiv_sandbox_egress_ca_cert egress-ca.crt
docker secret create daiv_sandbox_egress_ca_key egress-ca.key
```

!!! warning "Additional Secrets May Be Required"
    These are the minimal secrets for basic DAIV functionality. Check the [Environment Variables](../reference/env-variables.md) page for additional secrets needed for specific features or services.

### Step 2: Create `stack.yml` file

**Create your deployment configuration file**. This YAML file defines all services, networks, and volumes needed for DAIV.

!!! warning "Customize Environment Variables"
    **Replace all annotated values with your own configuration**. See the [Environment Variables](../reference/env-variables.md) page for complete configuration options.

!!! warning "Raise the open-file limit (`nofile`)"
    Each DAIV agent run holds many concurrent sockets (sandbox, LLM API, tracing, database, Redis, git-over-HTTPS). Docker's default soft limit of **1024** open files is too low and surfaces under load as `[Errno 24] Too many open files`. Swarm **ignores** service-level `ulimits`, so raise it on the Docker daemon of **each node** — add to `/etc/docker/daemon.json`:

    ```json
    {
      "default-ulimits": {
        "nofile": { "Name": "nofile", "Soft": 65536, "Hard": 524288 }
      }
    }
    ```

    then restart Docker (e.g. `systemctl restart docker`).

```yaml
x-app-environment-defaults: &app_environment_defaults
  # DJANGO
  DJANGO_SETTINGS_MODULE: daiv.settings.production
  DJANGO_ALLOWED_HOSTS: daiv.example.com,app,127.0.0.1 # (1)!
  DJANGO_REDIS_URL: redis://daiv_redis:6379/0
  DJANGO_REDIS_SESSION_URL: redis://daiv_redis:6379/1
  DAIV_EXTERNAL_URL: https://daiv.example.com # (2)!
  # DATABASE
  DB_NAME: daiv
  DB_USER: daiv_admin
  DB_HOST: daiv_db
  DB_SSLMODE: prefer
  # CODEBASE
  CODEBASE_CLIENT: gitlab
  CODEBASE_GITLAB_URL: https://gitlab.com # (3)!
  # EMAIL
  EMAIL_HOST: smtp.example.com
  EMAIL_PORT: 587
  EMAIL_USE_TLS: true
  # SANDBOX
  DAIV_SANDBOX_URL: http://sandbox:8000 # (4)!

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
    image: redis:8.6-alpine # (12)!
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
    image: ghcr.io/srtab/daiv:${DAIV_IMAGE_TAG:-main} # (5)!
    environment:
      <<: *app_environment_defaults
    secrets:
      - django_secret_key
      - db_password
      - codebase_gitlab_auth_token
      - codebase_gitlab_webhook_secret
      - daiv_sandbox_api_key
      - openrouter_api_key
      - email_host_password
      - allauth_client_id
      - allauth_client_secret
    networks:
      - internal
      - external
    ports:
      - "8000:8000"
    volumes:
      - media-volume:/home/daiv/data/media # (11)!
    deploy:
      <<: *deploy_defaults

  worker:
    image: ghcr.io/srtab/daiv:${DAIV_IMAGE_TAG:-main} # (5)!
    command: sh /home/daiv/start-worker default
    environment:
      <<: *app_environment_defaults
    secrets:
      - django_secret_key
      - db_password
      - codebase_gitlab_auth_token
      - codebase_gitlab_webhook_secret
      - daiv_sandbox_api_key
      - openrouter_api_key
      - email_host_password
    networks:
      - internal
    volumes:
      - media-volume:/home/daiv/data/media # (11)!
      # - ./custom-skills:/home/daiv/data/skills:ro  (10)
    healthcheck:
      test: grep -q 'db_worker' /proc/*/cmdline 2>/dev/null
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    deploy:
      <<: *deploy_defaults
      replicas: 1 # (9)!

  worker-interactive:
    image: ghcr.io/srtab/daiv:${DAIV_IMAGE_TAG:-main} # (5)!
    command: sh /home/daiv/start-worker interactive
    environment:
      <<: *app_environment_defaults
    secrets:
      - django_secret_key
      - db_password
      - codebase_gitlab_auth_token
      - codebase_gitlab_webhook_secret
      - daiv_sandbox_api_key
      - openrouter_api_key
      - email_host_password
    networks:
      - internal
    volumes:
      - media-volume:/home/daiv/data/media # (11)!
    healthcheck:
      test: grep -q 'db_worker' /proc/*/cmdline 2>/dev/null
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    deploy:
      <<: *deploy_defaults
      replicas: 1

  scheduler:
    image: ghcr.io/srtab/daiv:${DAIV_IMAGE_TAG:-main} # (5)!
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
    image: ghcr.io/srtab/daiv-sandbox:latest # (5)!
    environment:
      DAIV_SANDBOX_KEEP_TEMPLATE: true # (6)!
      DAIV_SANDBOX_EGRESS_CA_CERT_FILE: /run/secrets/daiv_sandbox_egress_ca_cert # (13)!
      DAIV_SANDBOX_EGRESS_CA_KEY_FILE: /run/secrets/daiv_sandbox_egress_ca_key
      DAIV_SANDBOX_REDIS_URL: redis://daiv_redis:6379/3
    networks:
      - internal
    secrets:
      - daiv_sandbox_api_key
      - daiv_sandbox_egress_ca_cert
      - daiv_sandbox_egress_ca_key
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock # (7)!
      - $HOME/.docker/config.json:/home/app/.docker/config.json # (8)!
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
  media-volume:
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
  email_host_password:
    external: true
  allauth_client_id:
    external: true
  allauth_client_secret:
    external: true
  daiv_sandbox_egress_ca_cert:
    external: true
  daiv_sandbox_egress_ca_key:
    external: true
```

1.   Replace `daiv.example.com` with your domain name, without the scheme (use `daiv.example.com`, not `https://daiv.example.com`). Keep `app` and `127.0.0.1` for internal service communication.
2.   Replace with your full domain URL including schema (e.g., `https://daiv.example.com`). Your GitLab sends webhooks to this URL, so it must be able to reach it
3.   Set to your GitLab instance URL (e.g., `https://gitlab.com` for GitLab.com)
4.   Points to the Sandbox service. Use `http://sandbox:8000` when deploying Sandbox in the same stack
5.   Continuous deploys track the `main` tag; keep the `${DAIV_IMAGE_TAG:-main}` form on every service so the Deploy workflow can roll back to a pinned per-commit tag (e.g. `DAIV_IMAGE_TAG=sha-XXXXXXX`)
6.   See [DAIV Sandbox documentation](https://github.com/srtab/daiv-sandbox) for configuration details
7.   **Required**: Sandbox needs Docker socket access to create isolated containers
8.   **Optional**: Remove this volume if you don't need private registry access
9.   **Scaling**: Increase `replicas` to handle more concurrent agent runs (e.g., `replicas: 3`). A worker runs one task to completion before claiming the next, so concurrency is the number of worker containers; each claims independently from the queues named in its command, and adding replicas scales DAIV's throughput with no architecture changes
10.  **Optional**: Uncomment to mount [custom global skills](../customization/agent-skills.md#custom-global-skills) that are available across all repositories
11.  **Required**: The web and worker containers must share this directory. It holds the [artifacts](../features/sessions.md#artifacts) (reports and other files) the agent publishes: `app` writes them for chat turns and serves them, and `worker` writes them for jobs and webhook runs. Any shared filesystem works; on a multi-node swarm use a network-backed volume driver
12.  **Redis 8 or later**: the agent's checkpoints use Redis search commands, which Redis 7 does not have, and they must stay on database `0`, the only one Redis can index
13.  **Required**: the egress CA you stored as secrets in [Step 1](#step-1-create-docker-secrets). The sandbox's egress proxy signs with it; without it, every agent run fails when the sandbox rejects the session

!!! note "Task queues"
    Background work is split in two: `default` carries agent runs, which hold a worker for as long as the run lasts, while `interactive` carries short user-visible work — session titles, run classification and notification delivery. The `worker` and `worker-interactive` services above each serve one of them, so a title never queues behind a run. A worker started with no queue argument serves every queue, which is fine for a small deployment as long as you accept that wait.

MCP tools are configured from the dashboard at `/dashboard/mcp-servers/` — see [MCP Tools](../customization/mcp-tools.md).

### Step 3: Deploy the stack

**Deploy your DAIV stack** by running this command from the directory containing your `stack.yml` file:

```bash
docker stack deploy -c stack.yml daiv
```

**Monitor deployment progress** with these commands:

```bash
# Check service status with full details
docker stack ps daiv --no-trunc

# Or check running containers
docker ps
```

!!! note "Deployment Time"
    **Services may take several minutes to become fully healthy**, especially during the initial deployment when images are being pulled and databases are being initialized.

### Step 4: Next steps

Your DAIV deployment is running. Follow the [Reverse Proxy](#reverse-proxy) guide below to configure external access, then proceed to [Platform Setup](platform-setup.md) to connect your first repository.

---

## :simple-docker: Docker Compose

**Docker Compose provides simpler deployment** suitable for development environments or smaller production setups. This method uses a single configuration file to manage all services.

**Prerequisites**

 * [Docker installed](https://docs.docker.com/engine/install/) with [Compose](https://docs.docker.com/compose/install/)
 * Internet connection to pull container images

### Step 1: Create the sandbox egress CA

**Create a certificate authority for the sandbox's egress proxy.** DAIV runs git inside the sandbox, and the sandbox reaches your Git platform only through a per-session proxy that adds the credentials, so the token never enters the sandbox. The proxy needs this CA. In the directory that will hold your `docker-compose.yml`, run:

```bash
mkdir certs
openssl req -x509 -newkey rsa:4096 -nodes -days 3650 -subj "/CN=daiv-sandbox-egress" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout certs/egress-ca.key -out certs/egress-ca.crt
chmod 644 certs/egress-ca.key
```

The sandbox service runs as UID 1001, so it needs read access to the key.

### Step 2: Create `docker-compose.yml` file

**Create your Docker Compose configuration**. This file defines all services and their configurations in a single place.

!!! note "Environment Variable Configuration"
    **Replace all annotated values with your specific configuration**. See the [Environment Variables](../reference/env-variables.md) page for additional options.

```yaml
x-app-defaults: &x_app_default
  image: ghcr.io/srtab/daiv:${DAIV_IMAGE_TAG:-main}
  restart: unless-stopped
  volumes:
    - media-volume:/home/daiv/data/media # (1)!
  ulimits: # (2)!
    nofile:
      soft: 65536
      hard: 524288
  environment:
    DJANGO_SETTINGS_MODULE: daiv.settings.production
    DJANGO_SECRET_KEY: secret-key # (3)!
    DJANGO_ALLOWED_HOSTS: daiv.example.com,app,127.0.0.1 # (4)!
    DJANGO_REDIS_URL: redis://redis:6379/0
    DJANGO_REDIS_SESSION_URL: redis://redis:6379/1
    DAIV_EXTERNAL_URL: https://daiv.example.com # (5)!
    # Database settings
    DB_HOST: db
    DB_NAME: daiv
    DB_USER: daiv
    DB_PASSWORD: daivpass # (6)!
    DB_SSLMODE: prefer
    # Codebase settings
    CODEBASE_CLIENT: gitlab
    CODEBASE_GITLAB_URL: https://gitlab.com # (7)!
    CODEBASE_GITLAB_AUTH_TOKEN: gitlab-auth-token # (8)!
    CODEBASE_GITLAB_WEBHOOK_SECRET: gitlab-webhook-secret # (9)!
    # LLM Providers settings
    OPENROUTER_API_KEY: openrouter-api-key # (10)!
    # Email settings
    EMAIL_HOST: smtp.example.com # (11)!
    EMAIL_PORT: 587
    EMAIL_USE_TLS: true
    # Sandbox settings
    DAIV_SANDBOX_API_KEY: daiv-sandbox-api-key # (12)!
    # Optional: Git platform sign-in (OAuth), also configurable at /dashboard/configuration/
    # ALLAUTH_CLIENT_ID: oauth-client-id
    # ALLAUTH_CLIENT_SECRET: oauth-client-secret

services:
  db:
    image: postgres:17.6
    restart: unless-stopped
    environment:
      POSTGRES_DB: daiv
      POSTGRES_USER: daiv
      POSTGRES_PASSWORD: daivpass # (13)!
    volumes:
      - db-volume:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U daiv -d daiv"]
      interval: 10s
      timeout: 10s
      start_period: 30s
      retries: 5

  redis:
    image: redis:8.6-alpine # (14)!
    restart: unless-stopped
    volumes:
      - redis-volume:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

  app:
    <<: *x_app_default
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
    command: sh /home/daiv/start-worker default
    # volumes:  (15)
    #   - media-volume:/home/daiv/data/media
    #   - ./custom-skills:/home/daiv/data/skills:ro
    healthcheck:
      test: ["CMD-SHELL", "grep -q 'db_worker' /proc/*/cmdline 2>/dev/null"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    ports: []
    deploy:
      replicas: 1 # (16)!
    depends_on:
      app:
        condition: service_healthy
        restart: true

  worker-interactive:
    <<: *x_app_default
    command: sh /home/daiv/start-worker interactive
    healthcheck:
      test: ["CMD-SHELL", "grep -q 'db_worker' /proc/*/cmdline 2>/dev/null"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    ports: []
    deploy:
      replicas: 1
    depends_on:
      app:
        condition: service_healthy
        restart: true

  scheduler:
    <<: *x_app_default
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
    group_add:
      - 987 # (17)!
    environment:
      DAIV_SANDBOX_API_KEY: daiv-sandbox-api-key # (18)!
      DAIV_SANDBOX_EGRESS_CA_CERT_FILE: /home/app/certs/egress-ca.crt
      DAIV_SANDBOX_EGRESS_CA_KEY_FILE: /home/app/certs/egress-ca.key
      DAIV_SANDBOX_REDIS_URL: redis://redis:6379/3
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./certs:/home/app/certs:ro # (19)!

volumes:
  db-volume:
    driver: local
  redis-volume:
    driver: local
  media-volume:
    driver: local
```

1.  **Required**: every service built from this anchor shares this directory. It holds the [artifacts](../features/sessions.md#artifacts) the agent publishes: `app` writes them for chat turns and serves them, and `worker` writes them for jobs and webhook runs
2.  **Raise the open-file limit**: each agent run holds many concurrent sockets (sandbox, LLM API, tracing, database, Redis, git-over-HTTPS). Docker's default soft limit of 1024 open files is too low and surfaces under load as `[Errno 24] Too many open files`. This raises `nofile` for every service built from this anchor
3.  **Generate a Django secret key**, a long random string such as the output of `openssl rand -hex 32`
4.  **Replace with your domain name**, without the scheme (e.g., `daiv.example.com`). Keep `app` and `127.0.0.1` for internal service communication
5.  **Include the full URL with scheme** (e.g., `https://daiv.example.com`). Your GitLab sends webhooks to this URL, so it must be able to reach it
6.  **Generate a secure random password** for the database, such as the output of `openssl rand -hex 16`
7.  **Set your GitLab instance URL** (e.g., `https://gitlab.com`)
8.  **The personal access token of DAIV's own GitLab user**, with the `api` scope and the Maintainer role on your projects. Use a dedicated account: DAIV ignores comments written by the account its token belongs to (see [Platform Setup](platform-setup.md#gitlab-configuration))
9.  **Generate a random webhook secret** for GitLab webhook validation, such as the output of `openssl rand -hex 32`
10. **Get an OpenRouter API key** for LLM model access. DAIV copies it into its database on the first start; to change it later, edit the provider under **Configuration → Providers** in the dashboard (see [LLM Providers](llm-providers.md))
11. **The SMTP server DAIV sends sign-in codes through.** Without one, replace the three `EMAIL_*` lines with `EMAIL_BACKEND: django.core.mail.backends.console.EmailBackend` and read the codes from `docker compose logs app`
12. **Generate a random API key** for Sandbox service authentication, such as the output of `openssl rand -hex 32`
13. **Use the same password** as defined in annotation 6
14. **Redis 8 or later**: the agent's checkpoints use Redis search commands, which Redis 7 does not have, and they must stay on database `0`, the only one Redis can index
15. **Optional**: Uncomment to mount [custom global skills](../customization/agent-skills.md#custom-global-skills) that are available across all repositories. A service-level `volumes:` replaces the anchor's list instead of merging with it, so keep the `media-volume` line
16. **Scaling**: Increase `replicas` to handle more concurrent agent runs (e.g., `replicas: 3`). A worker runs one task to completion before claiming the next, so concurrency is the number of worker containers; each claims independently from the queues named in its command, and adding replicas scales DAIV's throughput with no architecture changes
17. **Replace `987` with the group of the Docker socket**, the output of `stat -c '%g' /var/run/docker.sock`. The sandbox starts a container for each agent run through it
18. **Use the same API key** as defined in annotation 12
19. **The egress CA** from [Step 1](#step-1-create-the-sandbox-egress-ca)

!!! note "Task queues"
    Background work is split in two: `default` carries agent runs, which hold a worker for as long as the run lasts, while `interactive` carries short user-visible work — session titles, run classification and notification delivery. The `worker` and `worker-interactive` services above each serve one of them, so a title never queues behind a run. A worker started with no queue argument serves every queue, which is fine for a small deployment as long as you accept that wait.

MCP tools are configured from the dashboard at `/dashboard/mcp-servers/` — see [MCP Tools](../customization/mcp-tools.md).

### Step 3: Run the compose file

**Start all DAIV services** by running this command from the directory containing your `docker-compose.yml`:

```bash
docker compose up -d --wait
```

The command returns once every service is healthy. If one isn't, `docker compose logs <service>` shows why.

### Step 4: Create the first admin

**Create an admin account** to sign in to the dashboard:

```bash
docker compose exec app django-admin bootstrap_admin admin@example.com
```

Sign in at `/accounts/login/` with a one-time code DAIV emails to that address. See [Accounts & Roles](accounts.md) for Git platform sign-in and for adding users.

### Step 5: Next steps

Your DAIV instance is running. Continue with the [Reverse Proxy](#reverse-proxy) configuration below, then proceed to [Platform Setup](platform-setup.md) to connect your first repository.

---

## :simple-nginx: Reverse Proxy

**Configure a reverse proxy** to provide secure external access to your DAIV instance. This setup enables HTTPS access and proper domain routing.

**This guide covers Nginx configuration**. Basic Nginx knowledge is assumed.

!!! note "Nginx only"
    This guide provides an Nginx configuration only. Apache, Traefik and other reverse proxies have no configuration here.

**Prerequisites**

 * [Nginx installed](https://docs.nginx.com/nginx/admin-guide/installing-nginx/installing-nginx-open-source/)
 * Valid SSL certificate for your domain
 * Domain name pointing to your server

### Step 1: Configure Nginx

**Create a new Nginx configuration file** at `/etc/nginx/conf.d/daiv.conf` (path may vary by operating system).

**Add this configuration and customize the annotated values**:

```nginx
upstream daiv-instance {
  server internal-ip:8000; # (1)!
}

server {
  listen              443 ssl;
  listen              [::]:443 ssl;

  http2               on;

  server_name         daiv.example.com; # (2)!

  # SSL Configuration.
  # You can use this https://ssl-config.mozilla.org/ to generate
  # the correct ssl configuration for your server.
  ssl_certificate      /etc/pki/tls/certs/ssl.crt; # (3)!
  ssl_certificate_key  /etc/pki/tls/private/ssl.key; # (4)!

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

1.   **Set the internal IP** of your DAIV instance. Use `localhost` or `127.0.0.1` if running on the same server
2.   **Replace with your domain name** (e.g., `daiv.example.com`)
3.   **Update the SSL certificate path** - Location varies by operating system
4.   **Update the SSL certificate key path** - Location varies by operating system

### Step 2: Restart Nginx

**Apply the configuration changes** by restarting Nginx:

```bash
systemctl restart nginx
```

**Verify the configuration** by accessing your domain in a web browser. You should see the DAIV login page at `https://daiv.example.com/accounts/login/`.

!!! note "Sign-in needs HTTPS"
    DAIV's session cookie, which also carries the CSRF token, is a secure cookie, so a browser keeps it only on `https://` URLs and on `localhost`. Over plain `http://` to any other address, sign-in fails with **CSRF verification failed**.

---

## Next steps

Your DAIV instance is now running and accessible. Continue with:

1. **[Platform Setup](platform-setup.md)** — connect DAIV to your GitLab or GitHub repositories
2. **[LLM Providers](llm-providers.md)** — configure your LLM provider and API keys
3. **[Repository Config](../customization/repository-config.md)** — customize DAIV's behavior per repository
4. **[Site Configuration](../reference/site-configuration.md)** — manage settings from the admin UI instead of environment variables

!!! tip "First Login & User Management"
    On a fresh install with OAuth enabled, the first user to sign in via a social provider (GitHub or GitLab) is automatically assigned the **admin** role. Alternatively, you can bootstrap the initial admin via the management command, from the directory of your `docker-compose.yml`:

    ```bash
    docker compose exec app django-admin bootstrap_admin admin@example.com
    ```

    On Docker Swarm, run it in the app's container:

    ```bash
    docker exec -it $(docker ps -q -f name=daiv_app | head -n 1) django-admin bootstrap_admin admin@example.com
    ```

    This creates an admin user who can sign in using login-by-code (a one-time code sent by email) — useful when OAuth is not configured.

    After the initial admin is created, email signup stays disabled and social signup is off unless you set `DAIV_AUTH_SIGNUP_OPEN=true`, so new users must be created by an admin at `/accounts/users/`. Admins can assign users either the **admin** role (full access including user management) or the **member** role (dashboard, API keys, and MCP access).

    OAuth login can be configured and toggled on/off at any time via the **Configuration** page at `/dashboard/configuration/` under the **Authentication** section.
