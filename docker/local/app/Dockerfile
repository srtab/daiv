FROM python:3.12.7-slim-bookworm

LABEL maintainer="srtabs@gmail.com"

RUN apt-get update \
    && apt-get install -y \
        ca-certificates \
        # dependencies for building Python packages
        build-essential \
        # psycopg2 dependencies
        libpq-dev \
        # pillow
        libjpeg62 \
        libjpeg62-turbo-dev \
        zlib1g-dev \
        # Add more media types
        media-types \
        # other tools
        curl \
        vim \
        make \
        git \
        gettext \
        unzip \
        gnupg \
        ca-certificates \
        # Cleaning up unused files
    && apt-get purge -y --auto-remove \
        -o APT::AutoRemove::RecommendsImportant=0 \
        -o APT::Autoremove::SuggestsImportant=0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    # Create aplication specific user
    && addgroup --gid 1000 --system app \
    && adduser --home /home/app --uid 1000 --system --ingroup app app

USER app
WORKDIR /home/app/src

RUN curl -LsSf https://astral.sh/uv/install.sh | sh

ENV PYTHONPATH="/home/app/src/daiv"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH="/home/app/.venv/bin:/home/app/.cargo/bin/:$PATH"
ENV UV_PROJECT_ENVIRONMENT="/home/app/.venv"

COPY --chown=app:app pyproject.toml uv.lock ./
COPY --chown=app:app docker/local/app /home/app/docker

RUN uv sync \
    && echo 'alias compilemessages="django-admin compilemessages"' >> /home/app/.bashrc \
    && echo 'alias makemessages="django-admin makemessages --ignore=*/node_modules/* --ignore=.venv --no-location --no-wrap --all"' >> /home/app/.bashrc \
    && chmod +x /home/app/docker/entrypoint \
    && mkdir -p /home/app/data

ENTRYPOINT ["/home/app/docker/entrypoint"]
