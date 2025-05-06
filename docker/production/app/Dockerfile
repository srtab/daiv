#########################################################################################################
# Python compile image
#########################################################################################################
FROM python:3.13.3-slim-bookworm AS app-compiler

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  # dependencies for building Python packages
  build-essential \
  # psycopg dependencies
  libpq-dev

# Install uv
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#installing-uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Create a virtual environment and make it relocatable
RUN uv venv .venv --relocatable

# Install uv
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-editable --no-default-groups --no-install-project

#########################################################################################################
# Python build image
#########################################################################################################
FROM python:3.13.3-slim-bookworm AS python-builder

LABEL maintainer="srtabs@gmail.com"

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  # Psycopg dependencies
  libpq5 \
  # Used on healthcheckers
  curl \
  # Translations
  gettext \
  # Cleaning up unused files
  && apt-get purge -y --auto-remove \
  -o APT::AutoRemove::RecommendsImportant=0 \
  -o APT::Autoremove::SuggestsImportant=0 \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* /var/cache/* \
  # Create aplication specific user
  && addgroup --system --gid 1001 daiv \
  && adduser --system --ingroup daiv --uid 1001 --home /home/daiv daiv

ENV PATH="/home/daiv/.venv/bin:$PATH"
ENV PYTHONPATH="$PYTHONPATH:/home/daiv/app/"
ENV PYTHONUNBUFFERED=1

# Copy python compiled requirements
COPY --chown=daiv:daiv --from=app-compiler /.venv /home/daiv/.venv

# Copy application code
COPY --chown=daiv:daiv ./daiv /home/daiv/app
COPY --chown=daiv:daiv ./docker/production/app/entrypoint /home/daiv/entrypoint
COPY --chown=daiv:daiv ./docker/production/app/start-app /home/daiv/start-app
COPY --chown=daiv:daiv ./docker/production/app/start-worker /home/daiv/start-worker

USER daiv
WORKDIR /home/daiv

RUN chmod +x entrypoint start-app start-worker \
  && python -m compileall app \
  && django-admin compilemessages --ignore=.venv/**/locale \
  && mkdir -p data/tantivy_index data/media data/static

HEALTHCHECK --interval=10s --start-period=30s \
  CMD curl --fail http://127.0.0.1:8000/-/alive/ || exit 1

EXPOSE 8000

ENTRYPOINT ["/home/daiv/entrypoint"]
CMD ["/home/daiv/start-app"]
