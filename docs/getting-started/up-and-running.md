# Up and Running

This guide will walk you through the process of deploying DAIV using different methods (Docker Swarm and Docker Compose). By following these instructions, you will have a fully functional DAIV instance connected to your codebase, ready to assist your team with code insights and automation.

DAIV is designed to be deployed using container orchestration tools like Docker Swarm or Docker Compose, making it easy to set up and maintain. To run a complete DAIV instance, you'll need to deploy the following core services:

 * [PostgreSQL](https://www.postgresql.org/) with [pgvector](https://github.com/pgvector/pgvector) extension.
 * [Redis](https://redis.io/);
 * [DAIV Application](https://github.com/srtab/daiv);
 * [DAIV Worker](https://docs.celeryq.dev/);

Additionally, you can configure [DAIV Sandbox](https://github.com/srtab/daiv-sandbox) to allow DAIV to run arbitrary code or commands in an isolated environment:

 * [DAIV Sandbox](https://github.com/srtab/daiv-sandbox).

---

## :simple-swarm: Docker Swarm (*Recommended*)

This guide will walk you through the steps to deploy a DAIV using Docker Swarm with minimal configuration. The guide only explains how to deploy the stack to a single server, if you want to deploy to multiple servers you can check the [Docker Swarm documentation](https://docs.docker.com/engine/swarm/swarm-tutorial/) for more information.

This guide assumes you have a basic understanding of Docker Swarm.

**Prerequisites**
 * [Docker installed](https://docs.docker.com/engine/install/) with [Swarm enabled](https://docs.docker.com/engine/swarm/swarm-tutorial/).
 * Connection to the internet to pull the images.

### Step 1: Create Docker Secrets

Before deploying the stack, the following secrets need to be created:

* `django_secret_key`: A random secret key for Django. [Generate a Django secret key](https://djecrety.ir/).
* `db_password`: A random password for the database.
* `codebase_gitlab_auth_token`: A personal access token with `api` scope from your GitLab instance. DAIV will use this token to access the codebase.
* `codebase_gitlab_webhook_secret`: A random secret to be used as webhook secret for GitLab.
* `daiv_sandbox_api_key`: A random API key to authenticate requests to the Sandbox service.
* `openai_api_key`: An API key for OpenAI with access to `text-embedding-3-large` model. You can get one at https://platform.openai.com/api-keys.
* `openrouter_api_key`: An API key for OpenRouter. You can get one at https://openrouter.ai/settings/keys.

You can create the secrets using the following command (for more info, check the [Docker Secrets create documentation](https://docs.docker.com/reference/cli/docker/secret/create/)):

```bash
docker secret create django_secret_key <secret_key>
```

!!! warning
    These are the minimal secrets required to run DAIV. Check the [Environment Variables](environment-variables.md) page for more information about secrets required for other services.

### Step 2: Create `stack.yml` file

Here's an example of a `stack.yml` file that can be used to deploy DAIV.

!!! warning
    Remember to replace annotated environment variables with your own values.

    Check the [Environment Variables](environment-variables.md) page for more information about all supported environment variables.

<div class="annotate" markdown>

```yaml
x-app-environment-defaults: &app_environment_defaults
  # DJANGO
  DJANGO_SETTINGS_MODULE: daiv.settings.production
  DJANGO_ALLOWED_HOSTS: your-hostname.com,webapp,127.0.0.1 (1)
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
      - daiv_sandbox_api_key
      - openai_api_key
      - openrouter_api_key
    networks:
      - internal
      - external
    ports:
      - "8000:8000"
    volumes:
      - tantivy-volume:/home/daiv/data/tantivy_index_v1
      - embeddings-volume:/home/daiv/data/embeddings
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
      - daiv_sandbox_api_key
      - openai_api_key
      - openrouter_api_key
    networks:
      - internal
    volumes:
      - tantivy-volume:/home/daiv/data/tantivy_index_v1
      - embeddings-volume:/home/daiv/data/embeddings
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
  openai_api_key:
    external: true
  openrouter_api_key:
    external: true
```

</div>

1.   Replace `your-hostname.com` with your own hostname. Don't include the schema (e.g. `daiv.com`). Leave `webapp` and `127.0.0.1` as is to allow the app to be accessed from other services on the same host.
2.   Replace with your own hostname with schema included (e.g. `https://your-hostname.com`);
3.   Define with your GitLab instance URL (e.g. `https://gitlab.com`);
4.   This needs to point to the Sandbox service, if declared on the same stack define as `http://sandbox:8000`;
5.   It's advisable to replace with a specific version.
6.   Number of workers you want to run, this defines the number of parallel tasks that can be run at the same time.
7.   For more information about this option, check the [DAIV Sandbox](https://github.com/srtab/daiv-sandbox) documentation.
8.   Sandbox service needs access to the Docker socket to be able to create containers.
9.   Docker configuration file to be able to pull images from a private registry. If you don't need it, you can remove the volume.

### Step 3: Deploy the stack

To deploy the stack, make sure you are at the directory containing the stack file and run the following command:

```bash
docker stack deploy -c stack.yml daiv
```

To check the status of the stack, run the following command:

```bash
docker stack ps daiv --no-trunc
# or
docker ps
```

It can take a while for all services to be running and healthy.

### Step 4: Setup Webhooks

Now that the stack is deployed, you need to setup the webhooks for your GitLab instance. You can do this by going to the `daiv_daiv` service and running the following command:

```bash
docker exec -it $(docker ps -qf "name=daiv_daiv") django-admin setup_webhooks
```

### Step 5: Index the codebase

Finally, you need to index the codebase. DAIV will index all codebases it has access to.

You can index the codebase by going to the `daiv_daiv` service and running the following command:

```bash
docker exec -it $(docker ps -qf "name=daiv_daiv") django-admin update_index
```

!!! note
    You only need to run the `update_index` command on first deployment or when new codebases are added.

    After first run, the index will be **updated automatically** when a **new commit is pushed to the codebase**.


### Step 6: Next steps

Now that DAIV is running, check the [Reverse Proxy](#reverse-proxy) guide to help you configure a reverse proxy to access DAIV.

---

## :simple-docker: Docker Compose

This guide will walk you through the steps to deploy DAIV using Docker Compose.

**Prerequisites**
 * [Docker installed](https://docs.docker.com/engine/install/) with [Compose](https://docs.docker.com/compose/install/).
 * Connection to the internet to pull the images.

### Step 1: Create `docker-compose.yml` file

Here's an example of a `docker-compose.yml` file that can be used to run DAIV.

!!! info
    Remember to replace annotated environment variables with your own values. Check the [Environment Variables](environment-variables.md) page for more configuration options.

<div class="annotate" markdown>

```yaml
x-app-defaults: &x_app_default
  image: ghcr.io/srtab/daiv:latest
  restart: unless-stopped
  environment:
    - DJANGO_SECRET_KEY=secret-key (1)
    - DJANGO_ALLOWED_HOSTS=* (2)
    - DJANGO_REDIS_URL=redis://redis:6379/0
    - DJANGO_BROKER_URL=redis://redis:6379/0
    # Database settings
    - DB_HOST=db
    - DB_NAME=daiv
    - DB_USER=daiv
    - DB_PASSWORD=daivpass (3)
    - DB_SSLMODE=prefer
    # Codebase settings
    - CODEBASE_CLIENT=gitlab
    - CODEBASE_GITLAB_URL=http://gitlab:8929 (4)
    - CODEBASE_GITLAB_AUTH_TOKEN=gitlab-auth-token (5)
    - CODEBASE_GITLAB_WEBHOOK_SECRET=gitlab-webhook-secret (6)
    - CODEBASE_EMBEDDINGS_API_KEY=openai-api-key (7)
    # LLM Providers settings
    - OPENROUTER_API_KEY=openrouter-api-key (8)
    # Sandbox settings
    - DAIV_SANDBOX_API_KEY=daiv-sandbox-api-key (9)
  volumes:
    - tantivy-volume:/home/app/data/tantivy_index_v1
    - embeddings-volume:/home/app/data/embeddings
  depends_on:
    db:
      condition: service_healthy
      restart: true
    redis:
      condition: service_healthy
      restart: true
    sandbox:
      condition: service_healthy

services:
  db:
    image: pgvector/pgvector:pg17
    container_name: daiv-db
    restart: unless-stopped
    environment:
      - POSTGRES_DB=daiv
      - POSTGRES_USER=daiv
      - POSTGRES_PASSWORD=daivpass (11)
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
    command: redis-server --save "" --appendonly no
    restart: unless-stopped
    container_name: daiv-redis
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

  worker:
    <<: *x_app_default
    container_name: daiv-worker
    command: sh /home/app/docker/start-worker
    environment:
      - C_FORCE_ROOT=true
    ports: []

  sandbox:
    image: ghcr.io/srtab/daiv-sandbox:latest
    restart: unless-stopped
    container_name: daiv-sandbox
    environment:
      - DAIV_SANDBOX_API_KEY=daiv-sandbox-api-key (10)
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock

volumes:
  db-volume:
    driver: local
  tantivy-volume:
    driver: local
  embeddings-volume:
    driver: local

```

</div>

1.   [Generate a Django secret key](https://djecrety.ir/).
2.   Define your own hostname. Don't include the schema (e.g. `daiv.com`).
3.   Generate a random password.
4.   Define with your GitLab instance URL (e.g. `https://gitlab.com`).
5.   Generate a personal access token with `api` scope from your GitLab instance.
6.   Generate a random webhook secret.
7.   Go to OpenAI and generate an API key with access to `text-embedding-3-large` model.
8.   Go to OpenRouter and generate an API key.
9.   Generate a random Sandbox API key.
10.  Define with the same API key you generated for the app service.
11.  Define with the same password you generated for the database.

### Step 2: Run the compose file

To run the compose file, make sure you are at the directory containing the file and run the following command:

```bash
docker compose up -d
```

To check the status of the services, run the following command:

```bash
docker compose ps
```

### Step 3: Setup Webhooks

Now that the stack is deployed, you need to setup the webhooks for your GitLab instance. You can do this by going to the `app` service and running the following command:

```bash
docker compose exec -it app django-admin setup_webhooks
```

### Step 4: Index the codebase

Finally, you need to index the codebase. DAIV will index all codebases it has access to.

You can index the codebase by going to the `app` service and running the following command:

```bash
docker compose exec -it app django-admin update_index
```

!!! note
    You only need to run the `update_index` command on first deployment or when new codebases are added.

    After first run, the index will be **updated automatically** when a **new commit is pushed to the codebase**.


### Step 5: Next steps

Now that DAIV is running, check the [Reverse Proxy](#reverse-proxy) guide to help you configure a reverse proxy to access DAIV.

---

## :simple-nginx: Reverse Proxy

This guide will walk you through the steps to configure Nginx as a reverse proxy for DAIV.

It's assumed you have a basic understanding of Nginx.

!!! info "Contributions welcome!"
    Only the Nginx configuration is provided in this guide. Contributions to other reverse proxy configurations are welcome!

**Prerequisites**

 * [Nginx installed](https://docs.nginx.com/nginx/admin-guide/installing-nginx/installing-nginx-open-source/).

### Step 1: Configure Nginx

Create a new configuration file for DAIV `/etc/nginx/conf.d/daiv.conf`. The path to the configuration may vary depending on the Operating System you are using.

Add the following configuration and replace the values with your own:

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

1.   Define with the internal IP pointing to the service running DAIV. For instance, if you are running DAIV on the same server, you can use `localhost` or `127.0.0.1`.
2.   Define with your own hostname.
3.   Change to the path to your SSL certificate. The correct path depends on your operating system.
4.   Change to the path to your SSL certificate key. The correct path depends on your operating system.


### Step 2: Restart Nginx

Restart Nginx to apply the changes.

```bash
systemctl restart nginx
```
