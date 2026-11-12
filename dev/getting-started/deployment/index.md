# Deployment

This page deploys a self-hosted DAIV with Docker Swarm or Docker Compose, puts it behind an Nginx reverse proxy, and creates the first admin. Afterwards you have a DAIV instance ready to connect to your codebase.

To try DAIV on one machine first, follow the [Quickstart](https://docs.daivagent.com/dev/getting-started/quickstart/index.md).

## Requirements

| Requirement    | Version                                                                                                                                                                                                                                               |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Docker Engine  | The samples pin no minimum. Docker Swarm needs Swarm mode enabled on the host.                                                                                                                                                                        |
| Docker Compose | 2.17.0 or later, which provides the `docker compose` command and `depends_on.restart`. Only the Compose deployment needs it.                                                                                                                          |
| PostgreSQL     | 15 or later, the minimum of the Django release DAIV pins; the samples run `postgres:17.6`.                                                                                                                                                            |
| Redis          | 8 or later. The samples run `redis:8.6-alpine`. The agent's checkpoints use Redis search commands that Redis 7 lacks, and they must stay on database `0`; see [Redis](https://docs.daivagent.com/dev/reference/env-variables/#redis).                 |
| daiv-sandbox   | The samples pull `ghcr.io/srtab/daiv-sandbox:latest`. Update the sandbox together with DAIV: multi-repository sessions need a release with `repos_archive` support and path-scoped egress rules, and DAIV reports an error when the sandbox lacks it. |

You also need a domain name that points to your server, with an SSL certificate for it, and a way for your Git platform to reach `DAIV_EXTERNAL_URL`. Your Git platform sends webhooks to that URL.

## Services DAIV runs

DAIV needs these services. The samples below run all of them:

- **[PostgreSQL](https://www.postgresql.org/)** stores application data.
- **[Redis](https://redis.io/)** holds the cache, sessions and the agent's checkpoints.
- **[DAIV app](https://github.com/srtab/daiv)** (`app`) serves the web dashboard and the API.
- **[DAIV worker](https://docs.djangoproject.com/en/6.0/topics/tasks/)** (`worker` and `worker-interactive`) runs background tasks.
- **[DAIV scheduler](https://pypi.org/project/django-crontask/)** (`scheduler`) runs periodic tasks, among them adding webhooks to your GitLab projects and syncing repository access.
- **[DAIV sandbox](https://github.com/srtab/daiv-sandbox)** (`sandbox`) runs the agent's commands and git in isolated containers, through an egress proxy that holds the git credentials. See [Sandbox](https://docs.daivagent.com/dev/features/sandbox/index.md).

Task queues

Background work is split in two: `default` carries agent runs, which hold a worker for as long as the run lasts, while `interactive` carries short user-visible work: session titles, run classification and notification delivery. The `worker` and `worker-interactive` services each serve one of them, so a title never queues behind a run. A worker started with no queue argument serves every queue, which suits a small deployment that accepts that wait.

## Deploy with Docker Swarm

Docker Swarm gives you service discovery, load balancing and rolling updates, so it suits production. These steps deploy to a single server; to extend the stack to more servers, see the [Docker Swarm documentation](https://docs.docker.com/engine/swarm/swarm-tutorial/).

Before you start, [install Docker](https://docs.docker.com/engine/install/) and [enable Swarm](https://docs.docker.com/engine/swarm/swarm-tutorial/), and check the [requirements](#requirements).

1. Create the first seven Docker secrets listed in [Swarm secrets](#swarm-secrets), with this command (see [Docker secret create](https://docs.docker.com/reference/cli/docker/secret/create/)), replacing `<name>` and `<value>`:

   ```
   printf '%s' '<value>' | docker secret create <name> -
   ```

1. Create the egress CA. The sandbox reaches your Git platform only through a per-session proxy that adds the credentials, so the token never enters the sandbox, and the proxy signs with this CA:

   ```
   openssl req -x509 -newkey rsa:4096 -nodes -days 3650 -subj "/CN=daiv-sandbox-egress" \
     -addext "keyUsage=critical,keyCertSign,cRLSign" \
     -keyout egress-ca.key -out egress-ca.crt
   ```

1. Store the CA as the two secrets the stack reads:

   ```
   docker secret create daiv_sandbox_egress_ca_cert egress-ca.crt
   docker secret create daiv_sandbox_egress_ca_key egress-ca.key
   ```

1. On each node, add this to `/etc/docker/daemon.json`. Each agent run holds many concurrent sockets (sandbox, LLM API, tracing, database, Redis, git over HTTPS), and Docker's default soft limit of 1024 open files fails under load with `[Errno 24] Too many open files`. Swarm ignores service-level `ulimits`, so the daemon must raise `nofile`:

   ```
   {
     "default-ulimits": {
       "nofile": { "Name": "nofile", "Soft": 65536, "Hard": 524288 }
     }
   }
   ```

1. On each node, restart Docker:

   ```
   systemctl restart docker
   ```

1. Save the [Swarm stack file](#swarm-stack-file) as `stack.yml`, and replace each annotated value with your own. [Environment variables](https://docs.daivagent.com/dev/reference/env-variables/index.md) lists the options.

1. From the directory that holds `stack.yml`, deploy the stack:

   ```
   docker stack deploy -c stack.yml daiv
   ```

1. Wait until every service is healthy, which takes several minutes while Docker pulls the images and DAIV migrates the database:

   ```
   docker stack ps daiv --no-trunc
   ```

1. [Put DAIV behind Nginx](#put-daiv-behind-nginx), so you can reach it over HTTPS.

1. Create the first admin by running [`bootstrap_admin`](https://docs.daivagent.com/dev/reference/management-commands/#bootstrap_admin) in the app's container, replacing `admin@example.com` with the admin's email address:

   ```
   docker exec -it $(docker ps -q -f name=daiv_app | head -n 1) django-admin bootstrap_admin admin@example.com
   ```

1. Open `/accounts/login/` and sign in with the one-time code DAIV emails to that address.

If you turn on OAuth login before anyone signs in, the first Git platform sign-in becomes the admin instead. [The first admin](https://docs.daivagent.com/dev/getting-started/accounts/#the-first-admin) explains both paths, and [Accounts and roles](https://docs.daivagent.com/dev/getting-started/accounts/index.md) covers adding users.

### Swarm secrets

The stack reads these Docker secrets. Create the first seven rows in step 1, and the next two in steps 2 and 3. The last two are optional.

| Secret                           | Holds                                                                                                                                                                                     |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `django_secret_key`              | A random secret key for Django, such as the output of `openssl rand -hex 32`. Keep it backed up: see [Django](https://docs.daivagent.com/dev/reference/env-variables/#django).            |
| `db_password`                    | A random password for the PostgreSQL database.                                                                                                                                            |
| `codebase_gitlab_auth_token`     | A GitLab personal access token with the `api` scope. See [Platform setup](https://docs.daivagent.com/dev/getting-started/platform-setup/#gitlab-token-role-and-webhooks).                 |
| `codebase_gitlab_webhook_secret` | A random secret that GitLab sends with each webhook, for DAIV to validate.                                                                                                                |
| `daiv_sandbox_api_key`           | A random API key for the sandbox service.                                                                                                                                                 |
| `email_host_password`            | The SMTP password. DAIV authenticates only when `EMAIL_HOST_USER` is also set. The stack requires the secret to exist, so create it with any value if your relay needs no authentication. |
| `openrouter_api_key`             | An [OpenRouter API key](https://openrouter.ai/settings/keys).                                                                                                                             |
| `daiv_sandbox_egress_ca_cert`    | The egress CA certificate.                                                                                                                                                                |
| `daiv_sandbox_egress_ca_key`     | The egress CA private key.                                                                                                                                                                |
| `allauth_client_id`              | **Optional.** The OAuth client ID of your Git platform. See [Authentication](https://docs.daivagent.com/dev/reference/env-variables/#authentication).                                     |
| `allauth_client_secret`          | **Optional.** The OAuth client secret of your Git platform.                                                                                                                               |

A secret locks the matching field in the dashboard: DAIV reads the value from the secret and you cannot edit it under **Configuration** → **Authentication**. When you will [turn on OAuth login](https://docs.daivagent.com/dev/reference/site-configuration/#turn-on-oauth-login) in the dashboard instead, leave `allauth_client_id` and `allauth_client_secret` out of the `app` service's `secrets:` list and out of the top-level `secrets:`, as the stack file below does. To use them, create both secrets, then uncomment those lines.

For another provider's key, create a secret named after its variable in lowercase (`openai_api_key`, `anthropic_api_key` or `google_api_key`) add it to the `secrets:` list of the `app` service, which runs the migration that reads it, and declare it under the top-level `secrets:` with `external: true`. DAIV reads each provider key once, at its first start; see [Provider API keys](https://docs.daivagent.com/dev/getting-started/llm-providers/#provider-api-keys). For further variables you want as secrets, see [Environment variables](https://docs.daivagent.com/dev/reference/env-variables/index.md).

### Swarm stack file

```
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
  EMAIL_HOST_USER: smtp-user # (13)!
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
    image: redis:8.6-alpine # (11)!
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
      # - allauth_client_id  (14)
      # - allauth_client_secret  (14)
    networks:
      - internal
      - external
    ports:
      - "8000:8000"
    volumes:
      - media-volume:/home/daiv/data/media # (10)!
      # - ./custom-skills:/home/daiv/data/skills  (9)
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
      - media-volume:/home/daiv/data/media # (10)!
      # - ./custom-skills:/home/daiv/data/skills:ro  (9)
    healthcheck:
      test: grep -q 'db_worker' /proc/*/cmdline 2>/dev/null
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    deploy:
      <<: *deploy_defaults
      replicas: 1 # (8)!

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
      - media-volume:/home/daiv/data/media # (10)!
      # - ./custom-skills:/home/daiv/data/skills:ro  (9)
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
      DAIV_SANDBOX_EGRESS_CA_CERT_FILE: /run/secrets/daiv_sandbox_egress_ca_cert # (12)!
      DAIV_SANDBOX_EGRESS_CA_KEY_FILE: /run/secrets/daiv_sandbox_egress_ca_key
      DAIV_SANDBOX_REDIS_URL: redis://daiv_redis:6379/3
    networks:
      - internal
    secrets:
      - daiv_sandbox_api_key
      - daiv_sandbox_egress_ca_cert
      - daiv_sandbox_egress_ca_key
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock # (6)!
      - $HOME/.docker/config.json:/home/app/.docker/config.json # (7)!
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
  daiv_sandbox_egress_ca_cert:
    external: true
  daiv_sandbox_egress_ca_key:
    external: true
  # allauth_client_id:  (14)
  #   external: true
  # allauth_client_secret:  (14)
  #   external: true
```

1. Replace `daiv.example.com` with your domain name, without the scheme (`daiv.example.com`, not `https://daiv.example.com`). Keep `app` and `127.0.0.1` for communication between services.
1. Replace with your full URL, including the scheme (for example `https://daiv.example.com`). Your GitLab sends webhooks to this URL, so it must be able to reach it.
1. Set to your GitLab instance URL (for example `https://gitlab.com` for GitLab.com).
1. Points to the sandbox service. Use `http://sandbox:8000` when the sandbox runs in the same stack.
1. Continuous deploys track the `main` tag. Keep the `${DAIV_IMAGE_TAG:-main}` form on every service, so the Deploy workflow can roll back to a pinned per-commit tag (for example `DAIV_IMAGE_TAG=sha-XXXXXXX`).
1. **Required**: the sandbox needs Docker socket access to create isolated containers.
1. **Optional**: remove this volume if you don't need private registry access.
1. **Scaling**: increase `replicas` to handle more concurrent agent runs (for example `replicas: 3`). A worker runs one task to completion before it claims the next, so concurrency is the number of worker containers. Each claims independently from the queues named in its command.
1. **Optional**: uncomment all three lines to mount [custom global skills](https://docs.daivagent.com/dev/customization/agent-skills/#custom-global-skills) that are available across all repositories. `app` receives uploads from the dashboard, so it mounts the directory read-write; `worker` and `worker-interactive` run the agent, so they can mount it read-only.
1. **Required**: the web and worker containers must share this directory. It holds the [artifacts](https://docs.daivagent.com/dev/features/sessions/#artifacts) (reports and other files) the agent publishes: `app` writes them for chat turns and serves them, and `worker` writes them for jobs and webhook runs. Any shared filesystem works; on a multi-node swarm, use a network-backed volume driver.
1. Redis 8 or later; see [Requirements](#requirements).
1. **Required**: the egress CA you stored as secrets in step 3 of [Deploy with Docker Swarm](#deploy-with-docker-swarm). The sandbox's egress proxy signs with it; without it, every agent run fails when the sandbox rejects the session.
1. The SMTP user. DAIV sends the `email_host_password` secret only when this is set; delete the line if your relay needs no authentication. Without an SMTP server, replace the `EMAIL_*` lines with `EMAIL_BACKEND: django.core.mail.backends.console.EmailBackend` and read the sign-in code from `docker service logs daiv_app`.
1. **Optional**: uncomment the two `allauth_*` lines under `app`'s `secrets:` and the two under the top-level `secrets:` only after you create both secrets. DAIV then takes the OAuth client ID and secret from them and locks the fields under **Configuration** → **Authentication**. Leave them commented to set up OAuth login in the dashboard.

## Deploy with Docker Compose

Docker Compose runs every service from one file, which suits development and smaller production setups. Before you start, [install Docker](https://docs.docker.com/engine/install/) with [Compose](https://docs.docker.com/compose/install/), and check the [requirements](#requirements).

1. In the directory that will hold your `docker-compose.yml`, create the egress CA for the sandbox. The sandbox reaches your Git platform only through a per-session proxy that adds the credentials, so the token never enters the sandbox, and the proxy signs with this CA:

   ```
   mkdir certs
   openssl req -x509 -newkey rsa:4096 -nodes -days 3650 -subj "/CN=daiv-sandbox-egress" \
     -addext "keyUsage=critical,keyCertSign,cRLSign" \
     -keyout certs/egress-ca.key -out certs/egress-ca.crt
   ```

1. Make the key readable, because the sandbox service runs as UID 1001:

   ```
   chmod 644 certs/egress-ca.key
   ```

1. Save the [Compose file](#compose-file) as `docker-compose.yml`, and replace each annotated value with your own. [Environment variables](https://docs.daivagent.com/dev/reference/env-variables/index.md) lists the options.

1. Start the services:

   ```
   docker compose up -d --wait
   ```

   The command returns once every service is healthy. If one isn't, `docker compose logs <service>` shows why.

1. [Put DAIV behind Nginx](#put-daiv-behind-nginx), so you can reach it over HTTPS.

1. Create the first admin by running [`bootstrap_admin`](https://docs.daivagent.com/dev/reference/management-commands/#bootstrap_admin), replacing `admin@example.com` with the admin's email address:

   ```
   docker compose exec app django-admin bootstrap_admin admin@example.com
   ```

1. Open `/accounts/login/` and sign in with the one-time code DAIV emails to that address.

If you turn on OAuth login before anyone signs in, the first Git platform sign-in becomes the admin instead. [The first admin](https://docs.daivagent.com/dev/getting-started/accounts/#the-first-admin) explains both paths, and [Accounts and roles](https://docs.daivagent.com/dev/getting-started/accounts/index.md) covers adding users.

### Compose file

```
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
    # Optional: Git platform sign-in (OAuth). Setting these locks the fields at /dashboard/configuration/
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
    # volumes:  (15)
    #   - media-volume:/home/daiv/data/media
    #   - ./custom-skills:/home/daiv/data/skills
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

1. **Required**: every service built from this anchor shares this directory. It holds the [artifacts](https://docs.daivagent.com/dev/features/sessions/#artifacts) the agent publishes: `app` writes them for chat turns and serves them, and `worker` writes them for jobs and webhook runs.
1. **Raises the open-file limit** for every service built from this anchor. Each agent run holds many concurrent sockets (sandbox, LLM API, tracing, database, Redis, git over HTTPS), and Docker's default soft limit of 1024 open files fails under load with `[Errno 24] Too many open files`.
1. Generate a Django secret key, a long random string such as the output of `openssl rand -hex 32`.
1. Replace with your domain name, without the scheme (for example `daiv.example.com`). Keep `app` and `127.0.0.1` for communication between services.
1. Include the full URL with the scheme (for example `https://daiv.example.com`). Your GitLab sends webhooks to this URL, so it must be able to reach it.
1. Generate a random password for the database, such as the output of `openssl rand -hex 16`.
1. Set to your GitLab instance URL (for example `https://gitlab.com`).
1. The personal access token of DAIV's own GitLab user, with the `api` scope and the Maintainer role on your projects. Use a dedicated account: DAIV ignores comments written by the account its token belongs to. See [Platform setup](https://docs.daivagent.com/dev/getting-started/platform-setup/#gitlab-token-role-and-webhooks).
1. Generate a random webhook secret for GitLab webhook validation, such as the output of `openssl rand -hex 32`.
1. An OpenRouter API key for LLM access. DAIV copies it into its database on the first start. To change it later, edit the provider under **Configuration** → **Providers** in the dashboard; see [LLM providers](https://docs.daivagent.com/dev/getting-started/llm-providers/index.md).
1. The SMTP server DAIV sends sign-in codes through. Without one, replace the three `EMAIL_*` lines with `EMAIL_BACKEND: django.core.mail.backends.console.EmailBackend` and read the codes from `docker compose logs app`.
1. Generate a random API key for the sandbox service, such as the output of `openssl rand -hex 32`.
1. Use the same password as in annotation 6.
1. Redis 8 or later; see [Requirements](#requirements).
1. **Optional**: uncomment the `volumes:` block of `app`, `worker` and `worker-interactive` to mount [custom global skills](https://docs.daivagent.com/dev/customization/agent-skills/#custom-global-skills) that are available across all repositories. `app` receives uploads from the dashboard, so it mounts the directory read-write; the workers run the agent, so they can mount it read-only. A service-level `volumes:` replaces the anchor's list instead of merging with it, so keep the `media-volume` line.
1. **Scaling**: increase `replicas` to handle more concurrent agent runs (for example `replicas: 3`). A worker runs one task to completion before it claims the next, so concurrency is the number of worker containers. Each claims independently from the queues named in its command.
1. Replace `987` with the group of the Docker socket, the output of `stat -c '%g' /var/run/docker.sock`. The sandbox starts a container for each agent run through it.
1. Use the same API key as in annotation 12.
1. The egress CA from step 1 of [Deploy with Docker Compose](#deploy-with-docker-compose).

## Put DAIV behind Nginx

An Nginx reverse proxy gives your DAIV instance HTTPS access and domain routing. This page gives an Nginx configuration only; it has none for Apache, Traefik or other proxies.

DAIV needs HTTPS. Its session cookie, which also carries the CSRF token, is a secure cookie, so a browser keeps it only on `https://` URLs and on `localhost`. Over plain `http://` to any other address, sign-in fails with **CSRF verification failed**.

Before you start, [install Nginx](https://docs.nginx.com/nginx/admin-guide/installing-nginx/installing-nginx-open-source/), and have an SSL certificate for your domain and a domain name that points to your server.

1. Save the [Nginx configuration](#nginx-configuration) as `/etc/nginx/conf.d/daiv.conf`. The path may vary by operating system. Replace each annotated value with your own.

1. Restart Nginx:

   ```
   systemctl restart nginx
   ```

1. In a browser, open `https://daiv.example.com/accounts/login/`, replacing the domain with yours. You see the DAIV login page.

### Nginx configuration

```
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

1. Set the internal IP of your DAIV instance. Use `localhost` or `127.0.0.1` if Nginx runs on the same server.
1. Replace with your domain name (for example `daiv.example.com`).
1. Update the SSL certificate path. The location varies by operating system.
1. Update the SSL certificate key path. The location varies by operating system.

## Next steps

- **[Platform setup](https://docs.daivagent.com/dev/getting-started/platform-setup/index.md)** connects DAIV to your GitLab or GitHub repositories.
- **[LLM providers](https://docs.daivagent.com/dev/getting-started/llm-providers/index.md)** configures your LLM providers and API keys.
- **[Repository configuration](https://docs.daivagent.com/dev/customization/repository-config/index.md)** customizes DAIV's behavior per repository.
- **[Site configuration](https://docs.daivagent.com/dev/reference/site-configuration/index.md)** manages settings from the dashboard instead of environment variables.
- **[MCP tools](https://docs.daivagent.com/dev/customization/mcp-tools/index.md)** connects the agent to external MCP servers.
