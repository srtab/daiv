# Quickstart

This page gets you from a clean machine to a first merge request in about 15 minutes: you run DAIV on one Linux host with Docker Compose, connect a GitLab project, and let DAIV turn an issue into a merge request.

It is a trial setup: one worker, no email, no single sign-on. For a production install, or to connect GitHub, follow [Deployment](https://docs.daivagent.com/dev/getting-started/deployment/index.md) and [Platform Setup](https://docs.daivagent.com/dev/getting-started/platform-setup/index.md).

## Before you start

You need:

- **A Linux host with Docker Engine and Docker Compose v2.** About 3 GB of free disk: the DAIV images are about 1.1 GB, and the image the sandbox runs the agent in is another 1.1 GB, pulled on the first run.
- **An [OpenRouter API key](https://openrouter.ai/settings/keys).** To use another provider, see [LLM Providers](https://docs.daivagent.com/dev/getting-started/llm-providers/index.md).
- **A GitLab project** on GitLab.com or a self-managed GitLab, where you are a Maintainer or Owner.
- **A URL GitLab can reach DAIV at.** GitLab tells DAIV about new issues and comments with webhooks, so it must be able to open DAIV's port 8000:
  - **Self-managed GitLab on your network:** the host's address, such as `http://192.168.1.10:8000`, is enough.
  - **GitLab.com:** a public URL, such as a server with a domain behind a reverse proxy, or an HTTPS tunnel to port 8000 on your machine.

## 1. Create a GitLab account for DAIV

DAIV works as a GitLab user: it comments, pushes and opens merge requests as that user, and it ignores comments written by that user. Give DAIV its own account; if it uses your token, it ignores your comments, including the one that approves its plan.

1. **Create a user for DAIV**, for example `daiv`. On GitLab.com, sign up a second account; on a self-managed GitLab, an admin creates it under **Admin → Users → New user**.
1. **Add that user to your project** with the **Maintainer** role (**Manage → Members → Invite members**). DAIV only sets up webhooks on projects where its user is a Maintainer or Owner.
1. **Sign in as that user and create a personal access token** with the `api` scope (avatar → **Edit profile** → **Access tokens**). Copy it; GitLab shows it once.

Can't create a second account?

Use a token from your own account, and label issues `daiv-auto` instead of `daiv` in step 6: DAIV then plans and implements without waiting for an approval comment. You won't be able to talk to DAIV in comments.

## 2. Create the configuration

Create a directory for DAIV, and in it a certificate authority for the sandbox's egress proxy. DAIV runs git inside the [sandbox](https://docs.daivagent.com/dev/features/sandbox/index.md); the proxy is what lets those commands reach GitLab, adding the credentials so the token never enters the sandbox.

```
mkdir daiv && cd daiv
mkdir certs
openssl req -x509 -newkey rsa:4096 -nodes -days 3650 -subj "/CN=daiv-sandbox-egress" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout certs/egress-ca.key -out certs/egress-ca.crt
chmod 644 certs/egress-ca.key
```

The sandbox service runs as UID 1001, so it needs read access to the key.

Create the `.env` file. The command fills in the random secrets and the Docker socket's group for you:

```
cat > .env <<EOF
DAIV_EXTERNAL_URL=http://192.168.1.10:8000
CODEBASE_GITLAB_URL=https://gitlab.com
CODEBASE_GITLAB_AUTH_TOKEN=glpat-replace-me
OPENROUTER_API_KEY=sk-or-replace-me
DJANGO_SECRET_KEY=$(openssl rand -hex 32)
DB_PASSWORD=$(openssl rand -hex 16)
CODEBASE_GITLAB_WEBHOOK_SECRET=$(openssl rand -hex 32)
DAIV_SANDBOX_API_KEY=$(openssl rand -hex 32)
DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
EOF
chmod 600 .env
```

Then edit the first four lines:

| Variable                     | Set it to                                                                                                                                                                |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `DAIV_EXTERNAL_URL`          | The URL GitLab can reach DAIV at, without a trailing slash (see [Before you start](#before-you-start))                                                                   |
| `CODEBASE_GITLAB_URL`        | Your GitLab's URL, as DAIV's containers reach it                                                                                                                         |
| `CODEBASE_GITLAB_AUTH_TOKEN` | The token of DAIV's GitLab user, from step 1                                                                                                                             |
| `OPENROUTER_API_KEY`         | Your OpenRouter API key. DAIV copies it into its database on the first start; to change it later, edit the provider under **Configuration → Providers** in the dashboard |

Create `docker-compose.yml` next to it:

```
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
      DAIV_SANDBOX_REDIS_URL: redis://redis:6379/1
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock # (5)!
      - ./certs:/home/app/certs:ro

volumes:
  db:
  redis:
  media:
```

1. Prints sign-in emails to the app's log instead of sending them, so you need no mail server. [Deployment](https://docs.daivagent.com/dev/getting-started/deployment/index.md) shows how to configure SMTP.
1. DAIV needs Redis 8 or later: the agent's checkpoints use its search commands, which Redis 7 does not have.
1. One worker that serves every queue. [Deployment](https://docs.daivagent.com/dev/getting-started/deployment/index.md) splits agent runs and short interactive tasks across two.
1. Runs DAIV's periodic tasks, among them adding the webhook to your GitLab projects.
1. The sandbox starts a container for each agent run through the Docker socket.

## 3. Start DAIV

```
docker compose up -d --wait
```

The first start pulls about 1.2 GB of images and runs the database migrations; the command returns once every service is healthy, usually within three minutes. If it reports an unhealthy service, `docker compose logs app` shows why.

## 4. Sign in

Signing in is optional for the first merge request, but the dashboard is where you watch DAIV work. Create the first admin:

```
docker compose exec app django-admin bootstrap_admin you@example.com
```

Open <http://localhost:8000> on the DAIV host, enter that email address, and read the one-time code from the log:

```
docker compose logs app | grep -A2 "sign-in code"
```

Sign in over HTTPS or on localhost

DAIV's session cookie, which also carries the CSRF token, is a secure cookie, so a browser keeps it only on `https://` URLs and on `localhost`. Over plain `http://` to any other address, sign-in fails with **CSRF verification failed**. If DAIV runs on another machine, forward the port with `ssh -L 8000:localhost:8000 <host>` and open <http://localhost:8000>, or put DAIV behind HTTPS.

## 5. Connect your GitLab project

DAIV adds a webhook to every project its user maintains within five minutes. To add it now:

```
docker compose exec app django-admin setup_webhooks
```

In your project, **Settings → Webhooks** now lists `<DAIV_EXTERNAL_URL>/api/codebase/callbacks/gitlab/`. **Test → Push events** should answer with HTTP 204.

Self-managed GitLab and private addresses

A self-managed GitLab refuses webhooks to private network addresses by default (`Url is blocked: Requests to the local network are not allowed`). An admin allows them under **Admin → Settings → Network → Outbound requests**.

## 6. Get your first merge request

1. **Create an issue** in the project, for example *Add a `hello_world()` function that returns "Hello, world!"*, and add the `daiv` label. If the project has no `daiv` label yet, create it from the label picker.
1. **Wait for the plan.** DAIV reacts with 👀 within seconds and posts an implementation plan as a comment, usually within a minute.
1. **Approve it** with a comment that mentions DAIV's user, for example `@daiv proceed`.
1. **Wait for the merge request.** DAIV implements the plan and opens a merge request, usually within a few minutes. The first run takes longer while the sandbox pulls its image.

See [Issue Addressing](https://docs.daivagent.com/dev/features/issue-addressing/index.md) for what else you can do on an issue.

## If something goes wrong

| Symptom                                       | Likely cause                                                         | Fix                                                                                                                                                                      |
| --------------------------------------------- | -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Nothing happens after you add the label       | The webhook is missing, or GitLab can't reach `DAIV_EXTERNAL_URL`    | Run `setup_webhooks` (step 5) and check that DAIV's user is a Maintainer. Under **Settings → Webhooks → Edit → Recent events**, GitLab shows each delivery and its error |
| DAIV ignores your comment                     | You commented as DAIV's own user, or didn't mention its username     | Comment from your own account and mention DAIV's user                                                                                                                    |
| DAIV comments *An unexpected error occurred*  | The run failed                                                       | `docker compose logs worker sandbox` shows the error                                                                                                                     |
| Sign-in fails with *Server Error (500)*       | DAIV couldn't send the sign-in email                                 | Keep `EMAIL_BACKEND` as above, or configure SMTP as in [Deployment](https://docs.daivagent.com/dev/getting-started/deployment/index.md)                                  |
| Sign-in fails with *CSRF verification failed* | You opened DAIV over plain HTTP on an address other than `localhost` | See [Sign in](#4-sign-in)                                                                                                                                                |

To remove everything, including the database, run `docker compose down -v`, then delete the containers the sandbox started: `docker rm -f $(docker ps -aq --filter label=daiv.sandbox.type)`.

## Next steps

- **[Deployment](https://docs.daivagent.com/dev/getting-started/deployment/index.md)** — a production install: Docker Swarm, a reverse proxy with HTTPS, email and single sign-on
- **[Repository Config](https://docs.daivagent.com/dev/customization/repository-config/index.md)** — tune DAIV per repository with `.daiv.yml`
- **[Pull Request Assistant](https://docs.daivagent.com/dev/features/pull-request-assistant/index.md)** — mention DAIV on a merge request to get a review or a fix
