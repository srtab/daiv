Below is a comprehensive list of environment variables that DAIV supports. Many of these values have defaults or are optional, but you can override them for custom deployments. For security-sensitive values (like passwords, tokens), it’s recommended to use Docker secrets or other secure credential management solutions.

---

## Core Environment Variables

**General Application**

- **`DJANGO_SECRET_KEY`** (required): The secret key for Django’s cryptographic functions.
  _Example:_ `DJANGO_SECRET_KEY="super-secret-key"`

- **`DJANGO_DEBUG`** (default: `False`): Toggles Django’s debug mode. Set to `True` for development.
  _Example:_ `DJANGO_DEBUG="True"`

- **`DJANGO_ALLOWED_HOSTS`** (default: `*`): Comma-separated list of hosts/domain names that this site can serve.
  _Example:_ `DJANGO_ALLOWED_HOSTS="example.com,localhost"`

- **`ENVIRONMENT`** (default: none): Arbitrary descriptor of the environment (e.g., `production`, `staging`).
  _Example:_ `ENVIRONMENT="production"`

- **`VERSION`** (default: none): The application version. Primarily informational.
  _Example:_ `VERSION="0.1.0"`

- **`BRANCH`** (default: none): The application’s git branch name for release identification.
  _Example:_ `BRANCH="main"`

---

## Database Configuration

DAIV uses PostgreSQL with optional SSL and pgvector:

- **`DB_NAME`** (required): The PostgreSQL database name.
  _Example:_ `DB_NAME="db"`

- **`DB_USER`** (required): The PostgreSQL username.
  _Example:_ `DB_USER="dbuser"`

- **`DB_PASSWORD`** (required): The PostgreSQL user’s password.
  _Example:_ `DB_PASSWORD="dbpass"`

- **`DB_HOST`** (required): The host of the PostgreSQL server.
  _Example:_ `DB_HOST="db"`

- **`DB_PORT`** (default: `5432`): The PostgreSQL server port.
  _Example:_ `DB_PORT="5432"`

- **`DB_SSLMODE`** (default: `require` if not set otherwise): SSL mode for PostgreSQL connections. Options: `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full`.
  _Example:_ `DB_SSLMODE="prefer"`

---

## Broker & Caching (Celery & Redis)

- **`DJANGO_BROKER_URL`** (default: `"memory:///"` if not set): The Celery broker URL. Typically a Redis or AMQP URL.
  _Example:_ `DJANGO_BROKER_URL="redis://redis:6379/0"`

- **`DJANGO_BROKER_USE_SSL`** (default: `False`): If `True`, use SSL for broker connection.
  _Example:_ `DJANGO_BROKER_USE_SSL="True"`

- **`DJANGO_REDIS_URL`**: The Redis URL for caching and Celery result backend.
  _Example:_ `DJANGO_REDIS_URL="redis://redis:6379/1"`

---

## External Services & Integrations

**GitLab Integration**:

- **`CODEBASE_CLIENT`** (default: `gitlab`): The code host client, currently `gitlab` is supported.
  _Example:_ `CODEBASE_CLIENT="gitlab"`

- **`CODEBASE_GITLAB_URL`** (required if using GitLab): The URL of your GitLab instance.
  _Example:_ `CODEBASE_GITLAB_URL="https://gitlab.com"`

- **`CODEBASE_GITLAB_AUTH_TOKEN`** (required if using GitLab): A personal or project access token for GitLab API calls.
  _Example:_ `CODEBASE_GITLAB_AUTH_TOKEN="glpat-xyz"`

**Sandbox Integration**:

- **`DAIV_SANDBOX_URL`** (default: `"http://sandbox:8000"`): URL of the sandbox environment for running code and commands.
  _Example:_ `DAIV_SANDBOX_URL="http://sandbox:8000"`

- **`DAIV_SANDBOX_API_KEY`** (required): The API key for the sandbox service.
  _Example:_ `DAIV_SANDBOX_API_KEY="some-secret-key"`

---

## Gunicorn Settings (if using `start-app` script)

- **`GUNICORN_BIND`** (default: `0.0.0.0`): The interface/port that Gunicorn will bind to.
  _Example:_ `GUNICORN_BIND="0.0.0.0"`

- **`GUNICORN_PORT`** (default: `8000`): The port Gunicorn listens on.
  _Example:_ `GUNICORN_PORT="8000"`

- **`GUNICORN_TIMEOUT`** (default: `30`): Worker timeout in seconds.
  _Example:_ `GUNICORN_TIMEOUT="60"`

- **`GUNICORN_WORKERS`** (default: `1`): Number of Gunicorn worker processes.
  _Example:_ `GUNICORN_WORKERS="4"`

- **`GUNICORN_THREADS`** (default: `2`): Number of threads per worker.
  _Example:_ `GUNICORN_THREADS="4"`

---

## Other Variables

You may also set environment variables for development or debugging:

- **`DAIV_EXTERNAL_URL`**: Used by setup commands (e.g., webhook setup) to define the external publicly accessible URL of the DAIV instance.
  _Example:_ `DAIV_EXTERNAL_URL="http://localhost:8000"`

---

## Summary

To run DAIV successfully, at minimum you must provide:

- **`DJANGO_SECRET_KEY`**
- **`DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`**
- **`CODEBASE_GITLAB_AUTH_TOKEN`** and `CODEBASE_GITLAB_URL` if integrating with GitLab
- **`DAIV_SANDBOX_API_KEY`**

All other variables can be adjusted or left to defaults depending on your environment and security practices.

**Remember**: For sensitive information, consider using Docker secrets or other secure storage solutions rather than environment variables directly.
