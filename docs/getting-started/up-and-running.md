# Up and Running

This guide walks you through deploying DAIV using Docker Swarm or Docker Compose. **DAIV is an AI-powered development assistant** that helps teams with code insights, automated issue resolution, and development workflow automation. After completing this guide, you'll have a fully functional DAIV instance ready to connect to your codebase.

## What You'll Deploy

**DAIV requires several core services to function properly**. You'll deploy these services using container orchestration:

**Required Core Services:**

 * **[PostgreSQL](https://www.postgresql.org/)** with [pgvector](https://github.com/pgvector/pgvector) extension - Stores application data and vector embeddings;
 * **[Redis](https://redis.io/)** - Handles caching and message queueing;
 * **[DAIV Application](https://github.com/srtab/daiv)** - Main API;
 * **[DAIV Worker](https://docs.celeryq.dev/)** - Background task processor.

**Optional Service:**

 * **[DAIV Sandbox](https://github.com/srtab/daiv-sandbox)** - Isolated environment for running arbitrary code;
 * **[MCP Proxy](https://github.com/TBXark/mcp-proxy/)** - Proxy MCP server to run other MCP servers inside a container.

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
* **`codebase_gitlab_auth_token`** - GitLab personal access token with `api` scope (see [how to create one](configuration.md#step-1-create-gitlab-personal-access-token))
* **`codebase_gitlab_webhook_secret`** - Random secret for GitLab webhook validation
* **`codebase_embeddings_api_key`** - [OpenAI API key](https://platform.openai.com/api-keys) with access to `text-embedding-3-large` model
* **`daiv_sandbox_api_key`** - Random API key for Sandbox service authentication
* **`openrouter_api_key`** - [OpenRouter API key](https://openrouter.ai/settings/keys) for LLM access
* **`mcp_proxy_auth_token`** - Random API key for MCP Proxy service authentication

**Create each secret using this command** (see [Docker Secrets documentation](https://docs.docker.com/reference/cli/docker/secret/create/) for more details):

```bash
docker secret create django_secret_key <secret_key>
```

!!! warning "Additional Secrets May Be Required"
    These are the minimal secrets for basic DAIV functionality. Check the [Environment Variables](environment-variables.md) page for additional secrets needed for specific features or services.

### Step 2: Create `stack.yml` file

**Create your deployment configuration file**. This YAML file defines all services, networks, and volumes needed for DAIV.

!!! warning "Customize Environment Variables"
    **Replace all annotated values with your own configuration**. See the [Environment Variables](environment-variables.md) page for complete configuration options.

<div class="annotate" markdown>

```yaml
x-app-environment-defaults: &app_environment_defaults
  # DJANGO
  DJANGO_SETTINGS_MODULE: daiv.settings.production
  DJANGO_ALLOWED_HOSTS: your-hostname.com,app,127.0.0.1 (1)
  DJANGO_REDIS_URL: redis://daiv_redis:6379/0
  DJANGO_BROKER_URL: redis://daiv_redis:6379/0
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
    image: pgvector/pgvector:pg17
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
      - codebase_embeddings_api_key
      - daiv_sandbox_api_key
      - openrouter_api_key
      - mcp_proxy_auth_token
    networks:
      - internal
      - external
    ports:
      - "8000:8000"
    volumes:
      - tantivy-volume:/home/daiv/data/tantivy_index_v1
      - embeddings-volume:/home/daiv/data/embeddings
      - mcp-proxy-volume:/home/daiv/data/mcp-proxy
    deploy:
      <<: *deploy_defaults

  worker:
    image: ghcr.io/srtab/daiv:latest (5)
    command: sh /home/daiv/start-worker
    environment:
      <<: *app_environment_defaults
      CELERY_CONCURRENCY: 2 (6)
    secrets:
      - django_secret_key
      - db_password
      - codebase_gitlab_auth_token
      - codebase_gitlab_webhook_secret
      - codebase_embeddings_api_key
      - daiv_sandbox_api_key
      - openrouter_api_key
      - mcp_proxy_auth_token
    networks:
      - internal
    volumes:
      - tantivy-volume:/home/daiv/data/tantivy_index_v1
      - embeddings-volume:/home/daiv/data/embeddings
      - mcp-proxy-volume:/home/daiv/data/mcp-proxy
    healthcheck:
      test: celery -A daiv inspect ping
      interval: 10s
    deploy:
      <<: *deploy_defaults

  sandbox:
    image: ghcr.io/srtab/daiv-sandbox:latest (5)
    environment:
      DAIV_SANDBOX_KEEP_TEMPLATE: true (7)
    networks:
      - internal
    secrets:
      - daiv_sandbox_api_key
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock (8)
      - $HOME/.docker/config.json:/home/app/.docker/config.json (9)
    deploy:
      <<: *deploy_defaults

  mcp-proxy:
    image: ghcr.io/tbxark/mcp-proxy:latest
    networks:
      - internal
    volumes:
      - mcp-proxy-volume:/config
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
  tantivy-volume:
    driver: local
  embeddings-volume:
    driver: local
  mcp-proxy-volume:
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
  codebase_embeddings_api_key:
    external: true
  daiv_sandbox_api_key:
    external: true
  openrouter_api_key:
    external: true
  mcp_proxy_auth_token:
    external: true
```

</div>

1.   Replace `your-hostname.com` with your domain name. Don't include the schema (e.g., use `daiv.com` not `https://daiv.com`). Keep `app` and `127.0.0.1` for internal service communication.
2.   Replace with your full domain URL including schema (e.g., `https://your-hostname.com`)
3.   Set to your GitLab instance URL (e.g., `https://gitlab.com` for GitLab.com)
4.   Points to the Sandbox service. Use `http://sandbox:8000` when deploying Sandbox in the same stack
5.   **Recommended**: Replace `latest` with a specific version tag for production deployments
6.   Number of parallel worker processes. Adjust based on your server resources and expected workload
7.   See [DAIV Sandbox documentation](https://github.com/srtab/daiv-sandbox) for configuration details
8.   **Required**: Sandbox needs Docker socket access to create isolated containers
9.   **Optional**: Remove this volume if you don't need private registry access

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

!!! info "Deployment Time"
    **Services may take several minutes to become fully healthy**, especially during the initial deployment when images are being pulled and databases are being initialized.

### Step 4: ⏭️ Next steps

**Your DAIV deployment is now running!** Follow the [Reverse Proxy](#reverse-proxy) guide below to configure external access, then proceed to connect your first repository.

---

## :simple-docker: Docker Compose

**Docker Compose provides simpler deployment** suitable for development environments or smaller production setups. This method uses a single configuration file to manage all services.

**Prerequisites**

 * [Docker installed](https://docs.docker.com/engine/install/) with [Compose](https://docs.docker.com/compose/install/)
 * Internet connection to pull container images

### Step 1: Create `docker-compose.yml` file

**Create your Docker Compose configuration**. This file defines all services and their configurations in a single place.

!!! info "Environment Variable Configuration"
    **Replace all annotated values with your specific configuration**. See the [Environment Variables](environment-variables.md) page for additional options.

<div class="annotate" markdown>

```yaml
x-app-defaults: &x_app_default
  image: ghcr.io/srtab/daiv:latest
  restart: unless-stopped
  environment:
    DJANGO_SECRET_KEY: secret-key (1)
    DJANGO_ALLOWED_HOSTS: your-hostname.com,app,127.0.0.1 (2)
    DJANGO_REDIS_URL: redis://redis:6379/0
    DJANGO_BROKER_URL: redis://redis:6379/0
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
    CODEBASE_EMBEDDINGS_API_KEY: openai-api-key (7)
    # LLM Providers settings
    OPENROUTER_API_KEY: openrouter-api-key (8)
    # Sandbox settings
    DAIV_SANDBOX_API_KEY: daiv-sandbox-api-key (9)
    # MCP Proxy settings
    MCP_PROXY_AUTH_TOKEN: mcp-proxy-auth-token (13)
  volumes:
    - tantivy-volume:/home/app/data/tantivy_index_v1
    - embeddings-volume:/home/app/data/embeddings
    - mcp-proxy-volume:/home/app/data/mcp-proxy

services:
  db:
    image: pgvector/pgvector:pg17
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
    ports:
      - "5432:5432"

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
    ports:
      - "6379:6379"

  app:
    <<: *x_app_default
    container_name: daiv-app
    command: sh /home/app/docker/start-app
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
    container_name: daiv-worker
    command: sh /home/app/docker/start-worker
    environment:
      C_FORCE_ROOT: true
    ports: []
    depends_on:
      app:
        condition: service_healthy
        restart: true

  sandbox:
    image: ghcr.io/srtab/daiv-sandbox:latest
    restart: unless-stopped
    container_name: daiv-sandbox
    environment:
      DAIV_SANDBOX_API_KEY: daiv-sandbox-api-key (11)
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - $HOME/.docker/config.json:/home/app/.docker/config.json

  mcp-proxy:
    image: ghcr.io/tbxark/mcp-proxy:latest
    restart: unless-stopped
    container_name: daiv-mcp-proxy
    volumes:
      - mcp-proxy-volume:/config
    ports:
      - "9090:9090"
    depends_on:
      app:
        condition: service_healthy
        restart: true

volumes:
  db-volume:
    driver: local
  redis-volume:
    driver: local
  tantivy-volume:
    driver: local
  embeddings-volume:
    driver: local
  mcp-proxy-volume:
    driver: local
```

</div>

1.   **[Generate a Django secret key](https://djecrety.ir/)** - Use a cryptographically secure random string
2.   **Replace with your domain name** - Don't include schema (e.g., `daiv.com`)
3.   **Generate a secure random password** for the database
4.   **Set your GitLab instance URL** (e.g., `https://gitlab.com`)
5.   **Create a GitLab personal access token** with `api` scope permissions (see [how to create one](configuration.md#step-1-create-gitlab-personal-access-token))
6.   **Generate a random webhook secret** for GitLab webhook validation
7.   **Get an OpenAI API key** with access to `text-embedding-3-large` model
8.   **Get an OpenRouter API key** for LLM model access
9.   **Generate a random API key** for Sandbox service authentication
10.  **Use the same password** as defined in annotation 3
11.  **Use the same API key** as defined in annotation 9
12.  **Include the full URL with schema** (e.g., `https://your-hostname.com`)
13.  **Generate a random API key** for MCP Proxy service authentication

### Step 2: Run the compose file

**Start all DAIV services** by running this command from the directory containing your `docker-compose.yml`:

```bash
docker compose up -d
```

**Check service status** to ensure everything is running correctly:

```bash
docker compose ps
```

### Step 3: ⏭️ Next steps

**Your DAIV instance is now operational!** Continue with the [Reverse Proxy](#reverse-proxy) configuration below, then proceed to connect your first repository.

---

## :simple-nginx: Reverse Proxy

**Configure a reverse proxy** to provide secure external access to your DAIV instance. This setup enables HTTPS access and proper domain routing.

**This guide covers Nginx configuration**. Basic Nginx knowledge is assumed.

!!! info "Contributions Welcome"
    **Only Nginx configuration is provided currently**. Contributions for Apache, Traefik, and other reverse proxy configurations are welcome!

**Prerequisites**

 * [Nginx installed](https://docs.nginx.com/nginx/admin-guide/installing-nginx/installing-nginx-open-source/)
 * Valid SSL certificate for your domain
 * Domain name pointing to your server

### Step 1: Configure Nginx

**Create a new Nginx configuration file** at `/etc/nginx/conf.d/daiv.conf` (path may vary by operating system).

**Add this configuration and customize the annotated values**:

<div class="annotate" markdown>

```nginx
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
    listen [::]:80 default_server;

    return 301 https://$host$request_uri;
}
```

</div>

1.   **Set the internal IP** of your DAIV instance. Use `localhost` or `127.0.0.1` if running on the same server
2.   **Replace with your domain name** (e.g., `daiv.example.com`)
3.   **Update the SSL certificate path** - Location varies by operating system
4.   **Update the SSL certificate key path** - Location varies by operating system

### Step 2: Restart Nginx

**Apply the configuration changes** by restarting Nginx:

```bash
systemctl restart nginx
```

**Verify the configuration** by accessing your domain in a web browser. You should see the DAIV interface.

---

## 🚀 Final Steps and Repository Configuration

**Congratulations! Your DAIV instance is now running and accessible.** To start using DAIV with your repositories, follow these essential next steps:

### 1. Connect Your First Repository

**Your next step is connecting DAIV to your GitLab repositories**. This process involves:

- Creating GitLab personal access tokens
- Configuring repository webhooks
- Indexing repository content
- Setting up automated workflows\

**📖 Follow the complete repository setup guide**: [Repository Configuration](configuration.md)

### 2. What You Can Do After Configuration

**Once your repository is connected, DAIV will automatically**:

- **Respond to issues** - DAIV analyzes issues and suggests solutions or implementation plans
- **Review pull requests** - Automated code review and suggestions for improvements
- **Address pipeline failures** - Investigates CI/CD failures and proposes fixes
- **Answer code questions** - Provides context-aware responses about your codebase

### 3. Monitoring Your Instance

**Keep track of your DAIV deployment**:

```bash
# Check service health (Docker Swarm)
docker stack ps daiv

# Check service health (Docker Compose)
docker compose ps

# View application logs
docker logs <container_name>
```

### 4. Getting Help

**If you encounter issues during setup**:

- **Check the logs** for error messages and debugging information
- **Review the [Environment Variables](environment-variables.md)** for configuration options
- **Verify network connectivity** between services and external APIs
- **Ensure all secrets and API keys** are valid and have proper permissions
- **Ask for help** on the [GitHub Discussions](https://github.com/srtab/daiv/discussions)
