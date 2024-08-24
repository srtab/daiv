# Docker production image

This folder contains the required files to build the application image production ready.

## Envs

**Django:**

```bash
# [Required] Django settings to be loaded. This value tipically change by environment.
# Examples:
#     DJANGO_SETTINGS_MODULE=daiv.settings.development
#     DJANGO_SETTINGS_MODULE=daiv.settings.production
DJANGO_SETTINGS_MODULE=daiv.settings.production

# [Required] Django only serves if host or hosts are declared in this variable.
# Comma separated hosts for more than one.
DJANGO_ALLOWED_HOSTS

# [Required][Secret] Django secret key used to encrypt session and other data.
# To generate a new one go to: https://djecrety.ir/.
# IMPORTANT: Don't share secret keys between environments.
DJANGO_SECRET_KEY

# [Required] Redis string connection. More info: https://bit.ly/3fusjTh.
# Example:
#     DJANGO_REDIS_URL=redis://redis:6379/0
#     DJANGO_REDIS_URL=rediss://password@redis:6379/0
DJANGO_REDIS_URL

# [Secret] Redis password. Default: None
DJANGO_REDIS_PASSWORD

# [Required][Secret] String connection of broker to be used with Celery workers.
# Example:
#     DJANGO_BROKER_URL=amqp://guest:guest@rabbitmq//
DJANGO_BROKER_URL

# Logging level. Default: INFO
DJANGO_LOGGING_LEVEL

# Host of service relay that will send emails. Default: localhost.
DJANGO_EMAIL_HOST

# User of email relay.
DJANGO_EMAIL_HOST_USER

# [Secret] Password of email relay.
DJANGO_EMAIL_HOST_PASSWORD

# Port of service relay that will send emails. Default: 25.
DJANGO_EMAIL_PORT

# Use ssl on email relay connection. Default: False.
DJANGO_EMAIL_USE_TLS
```

**Gunicorn:**

```bash
# Address (host:port) where to bind the gunicorn. Define host as "0.0.0.0" to accept connections from any IP. Default: 0.0.0.0:8000.
GUNICORN_BIND

# Timeout for workers execution. After timeout, worker are killed and restarted. Default: 65.
GUNICORN_TIMEOUT

# The number of worker processes for handling requests. A positive integer generally in the 4 x $(NUM_CORES) - 2 range. Default 4 x (nproc --all) - 2.
GUNICORN_WORKERS
```

**Celery:**

```bash
# Celery log level. Options: DEBUG|INFO|WARNING|ERROR|CRITICAL|FATAL. Default: INFO.
CELERY_LOGLEVEL

# Number of child processes processing the queue. Default: 4 x (nproc --all) - 2).
CELERY_CONCURRENCY
```

**Database:**

```bash
# Database hostname. Default: localhost
DB_HOST

# [Required] Database name.
DB_NAME

# [Required] Database user.
DB_USER

# [Required][Secret] Database password.
DB_PASSWORD

# Database port. Default: 5432
DB_PORT

# Max database connection age. Change this value for production use. Default: 0
DB_CONN_MAX_AGE

# Database SSL mode, only for postgresql engines. Default: require
DB_SSLMODE
```

**Others**

```bash
# Project version. Default: None.
VERSION

# Project branch. Default: None.
BRANCH
```
