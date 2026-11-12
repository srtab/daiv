---
description: "Try DAIV in about 15 minutes: run it on one Linux machine with Docker Compose, connect a GitLab project, and turn a labelled issue into a merge request."
---

# Quickstart

This page gets you from a clean machine to a first merge request in about 15 minutes: you run DAIV on one Linux host with Docker Compose, connect a GitLab project, and let DAIV turn an issue into a merge request.

It is a trial setup: one worker, no email, no single sign-on. For a production install, or to connect GitHub, follow [Deployment](deployment.md) and [Platform setup](platform-setup.md).

## Before you start

You need:

- **A Linux host with Docker Engine and Docker Compose v2.** About 3 GB of free disk: the DAIV images are about 1.1 GB, and the image the sandbox runs the agent in is another 1.1 GB, pulled on the first run.
- **An [OpenRouter API key](https://openrouter.ai/settings/keys).** To use another provider, see [LLM providers](llm-providers.md).
- **A GitLab project** on GitLab.com or a self-managed GitLab, where you are a Maintainer or Owner.
- **A URL GitLab can reach DAIV at.** GitLab tells DAIV about new issues and comments with webhooks, so it must be able to open DAIV's port 8000:
    - **Self-managed GitLab on your network:** the host's address, such as `http://192.168.1.10:8000`, is enough.
    - **GitLab.com:** a public URL, such as a server with a domain behind a reverse proxy, or an HTTPS tunnel to port 8000 on your machine.

## Create a GitLab account for DAIV

DAIV works as a GitLab user: it comments, pushes and opens merge requests as that user, and it ignores comments written by that user. Give DAIV its own account; if it uses your token, it ignores your comments, including the one that approves its plan.

!!! tip "Can't create a second account?"
    Use a token from your own account, and label issues `daiv-auto` instead of `daiv` in [Get your first merge request](#get-your-first-merge-request): DAIV then plans and implements without waiting for an approval comment. You won't be able to talk to DAIV in comments.

1. Create a user for DAIV, for example `daiv`. On GitLab.com, sign up a second account; on a self-managed GitLab, an admin creates it under **Admin → Users → New user**.
2. In your project, add that user with the **Maintainer** role under **Manage → Members → Invite members**. DAIV only sets up webhooks on projects where its user is a Maintainer or Owner.
3. Sign in to GitLab as that user.
4. Select your avatar → **Edit profile** → **Access tokens**, and create a personal access token with the `api` scope.
5. Copy the token. GitLab shows it once.

## Create the configuration

Create a directory for DAIV, and in it a certificate authority for the sandbox's egress proxy. DAIV runs git inside the [sandbox](../features/sandbox.md); the proxy is what lets those commands reach GitLab, adding the credentials so the token never enters the sandbox.

```bash
mkdir daiv && cd daiv
mkdir certs
openssl req -x509 -newkey rsa:4096 -nodes -days 3650 -subj "/CN=daiv-sandbox-egress" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout certs/egress-ca.key -out certs/egress-ca.crt
```

The sandbox service runs as UID 1001, so it needs read access to the key. Make the key readable:

```bash
chmod 644 certs/egress-ca.key
```

Create the `.env` file. The command fills in the random secrets and the Docker socket's group for you:

```bash
cat > .env <<EOF
DAIV_EXTERNAL_URL=http://192.168.1.10:8000
CODEBASE_GITLAB_URL=https://gitlab.com
CODEBASE_GITLAB_AUTH_TOKEN=<gitlab-token>
OPENROUTER_API_KEY=<openrouter-api-key>
DJANGO_SECRET_KEY=$(openssl rand -hex 32)
DB_PASSWORD=$(openssl rand -hex 16)
CODEBASE_GITLAB_WEBHOOK_SECRET=$(openssl rand -hex 32)
DAIV_SANDBOX_API_KEY=$(openssl rand -hex 32)
DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
EOF
chmod 600 .env
```

Then replace the first four values. `<gitlab-token>` and `<openrouter-api-key>` are placeholders for the values of `CODEBASE_GITLAB_AUTH_TOKEN` and `OPENROUTER_API_KEY`:

| Variable | Set it to |
|----------|-----------|
| `DAIV_EXTERNAL_URL` | The URL GitLab can reach DAIV at, without a trailing slash (see [Before you start](#before-you-start)) |
| `CODEBASE_GITLAB_URL` | Your GitLab's URL, as DAIV's containers reach it |
| `CODEBASE_GITLAB_AUTH_TOKEN` | The token of DAIV's GitLab user, from [Create a GitLab account for DAIV](#create-a-gitlab-account-for-daiv) |
| `OPENROUTER_API_KEY` | Your OpenRouter API key. DAIV reads it once, on the first start; see [Provider API keys](llm-providers.md#provider-api-keys) |

Create `docker-compose.yml` next to it:

```yaml
x-daiv: &daiv
  image: ghcr.io/srtab/daiv:${DAIV_IMAGE_TAG:-main}
  restart: unless-stopped
  env_file: .env
  environment:
    DJANGO_SETTINGS_MODULE: daiv.settings.production
    DJANGO_REDIS_URL: redis://redis:6379/0
    DB_HOST: db
    DB_NAME: daiv
    DB_USER: daiv
    DB_SSLMODE: prefer
    CODEBASE_CLIENT: gitlab
    EMAIL_BACKEND: django.core.mail.backends.console.EmailBackend # (1)!
  volumes:
    - media:/home/daiv/data/media
  ulimits:
    nofile:
      soft: 65536
      hard: 524288

x-background: &background
  <<: *daiv
  healthcheck:
    test: ["CMD-SHELL", "grep -q -e db_worker -e crontask /proc/*/cmdline 2>/dev/null"]
    interval: 30s
  depends_on:
    app:
      condition: service_healthy

services:
  db:
    image: postgres:17.6
    restart: unless-stopped
    environment:
      POSTGRES_DB: daiv
      POSTGRES_USER: daiv
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - db:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U daiv -d daiv"]
      interval: 10s
      retries: 5

  redis:
    image: redis:8.6-alpine # (2)!
    restart: unless-stopped
    volumes:
      - redis:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      retries: 5

  app:
    <<: *daiv
    ports:
      - "8000:8000"
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy

  worker:
    <<: *background
    command: sh /home/daiv/start-worker # (3)!

  scheduler:
    <<: *background
    command: sh /home/daiv/start-crontask # (4)!

  sandbox:
    image: ghcr.io/srtab/daiv-sandbox:latest
    restart: unless-stopped
    group_add:
      - ${DOCKER_GID}
    environment:
      DAIV_SANDBOX_API_KEY: ${DAIV_SANDBOX_API_KEY}
      DAIV_SANDBOX_EGRESS_CA_CERT_FILE: /home/app/certs/egress-ca.crt
      DAIV_SANDBOX_EGRESS_CA_KEY_FILE: /home/app/certs/egress-ca.key
      DAIV_SANDBOX_REDIS_URL: redis://redis:6379/3
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock # (5)!
      - ./certs:/home/app/certs:ro

volumes:
  db:
  redis:
  media:
```

1.  Prints sign-in emails to the app's log instead of sending them, so you need no mail server. [Deployment](deployment.md) shows how to configure SMTP.
2.  DAIV needs Redis 8 or later: the agent's checkpoints use its search commands, which Redis 7 does not have.
3.  One worker that serves every queue. [Deployment](deployment.md) splits agent runs and short interactive tasks across two.
4.  Runs DAIV's periodic tasks, among them adding the webhook to your GitLab projects.
5.  The sandbox starts a container for each agent run through the Docker socket.

## Start DAIV

Start the stack from the directory that holds `docker-compose.yml`:

```bash
docker compose up -d --wait
```

The first start pulls about 1.2 GB of images and runs the database migrations; the command returns once every service is healthy, usually within three minutes. If it reports an unhealthy service, `docker compose logs app` shows why.

## Sign in

Signing in is optional for the first merge request, but the dashboard is where you watch DAIV work.

!!! warning "Sign in over HTTPS or on localhost"
    DAIV's session cookie, which also carries the CSRF token, is a secure cookie, so a browser keeps it only on `https://` URLs and on `localhost`. Over plain `http://` to any other address, sign-in fails with **CSRF verification failed**. If DAIV runs on another machine, forward the port with `ssh -L 8000:localhost:8000 <host>` and open `http://localhost:8000`, or put DAIV behind HTTPS.

1. Create the first admin with [`bootstrap_admin`](../reference/management-commands.md#bootstrap_admin), replacing `you@example.com` with your email address:

    ```bash
    docker compose exec app django-admin bootstrap_admin you@example.com
    ```

2. On the DAIV host, open `http://localhost:8000/accounts/login/`.
3. Enter that email address.
4. Select **Continue with email**.
5. Read the one-time code from the log:

    ```bash
    docker compose logs app | grep -A2 "sign-in code"
    ```

6. Enter the code.
7. Select **Verify & sign in**.

## Connect your GitLab project

DAIV adds a webhook to every project its user maintains within five minutes. To add it now:

1. Run [`setup_webhooks`](../reference/management-commands.md#setup_webhooks):

    ```bash
    docker compose exec app django-admin setup_webhooks
    ```

2. In your project, open **Settings → Webhooks**. It lists `<DAIV_EXTERNAL_URL>/api/codebase/callbacks/gitlab/`, with `<DAIV_EXTERNAL_URL>` replaced by your `DAIV_EXTERNAL_URL` value.
3. Select **Test → Push events**. GitLab shows HTTP 204.

!!! note "Self-managed GitLab and private addresses"
    A self-managed GitLab refuses webhooks to private network addresses by default (`Url is blocked: Requests to the local network are not allowed`). An admin allows them under **Admin → Settings → Network → Outbound requests**.

## Get your first merge request

1. In the project, create an issue, for example *Add a `hello_world()` function that returns "Hello, world!"*.
2. Add the `daiv` label to the issue. If the project has no `daiv` label yet, create it from the label picker.
3. Wait for the plan. DAIV reacts to the issue with the eyes emoji (👀) within seconds, and posts an implementation plan as a comment, usually within a minute.
4. Approve the plan with a comment that mentions DAIV's user, for example `@daiv proceed`.
5. Wait for the merge request. DAIV implements the plan and opens a merge request, usually within a few minutes. The first run takes longer while the sandbox pulls its image.

See [Issue Addressing](../features/issue-addressing.md) for what else you can do on an issue.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Nothing happens after you add the label | The webhook is missing, or GitLab can't reach `DAIV_EXTERNAL_URL` | Run `setup_webhooks` as in [Connect your GitLab project](#connect-your-gitlab-project) and check that DAIV's user is a Maintainer. Under **Settings → Webhooks → Edit → Recent events**, GitLab shows each delivery and its error |
| DAIV ignores your comment | You commented as DAIV's own user, or didn't mention its username | Comment from your own account and mention DAIV's user |
| DAIV comments *An unexpected error occurred* | The run failed | `docker compose logs worker sandbox` shows the error |
| Sign-in fails with *Server Error (500)* | DAIV couldn't send the sign-in email | Keep `EMAIL_BACKEND` as above, or configure SMTP as in [Deployment](deployment.md) |
| Sign-in fails with *CSRF verification failed* | You opened DAIV over plain HTTP on an address other than `localhost` | See [Sign in](#sign-in) |

## Remove DAIV

Remove DAIV and everything it stored when you are done with the trial.

!!! warning "This deletes the database"
    `docker compose down -v` removes the volumes, including the database volume `db`: every user, session and setting is lost, and you cannot restore them.

1. From the directory that holds `docker-compose.yml`, remove the containers and volumes:

    ```bash
    docker compose down -v
    ```

2. Remove the containers the sandbox started:

    ```bash
    docker rm -f $(docker ps -aq --filter label=daiv.sandbox.type)
    ```

## Next steps

- **[Deployment](deployment.md)** — a production install: Docker Swarm, a reverse proxy with HTTPS, email and single sign-on
- **[Repository configuration](../customization/repository-config.md)** — tune DAIV per repository with `.daiv.yml`
- **[Pull Request Assistant](../features/pull-request-assistant.md)** — mention DAIV on a merge request to get a review or a fix
