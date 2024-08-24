FROM python:3.12.4-slim-bookworm

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

ENV PYTHONPATH=$PYTHONPATH:/home/app/src/daiv
ENV PYTHONUNBUFFERED=1
# this prevents Python from writing out pyc files
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH=/home/app/.local/bin:$PATH
ENV PIPENV_PIPFILE=/home/app/src/Pipfile

COPY --chown=app:app Pipfile Pipfile.lock /home/app/src/

RUN python -m pip install --user -U pip wheel \
    && python -m pip install --user -U pipenv \
    && pipenv sync --dev

COPY --chown=app:app docker/local/app /home/app/docker

RUN echo 'alias makemigrations="pipenv run django-admin makemigrations"' >> /home/app/.bashrc \
    && echo 'alias migrate="pipenv run django-admin migrate"' >> /home/app/.bashrc \
    && echo 'alias compilemessages="pipenv run django-admin compilemessages"' >> /home/app/.bashrc \
    && echo 'alias makemessages="pipenv run django-admin makemessages --ignore=*/node_modules/* --ignore=.venv --no-location --no-wrap --all"' >> /home/app/.bashrc \
    && echo 'alias makemessagesjs="pipenv run django-admin makemessages -d djangojs --ignore=*/node_modules/* --ignore=.venv -e ts --no-wrap --all"' >> /home/app/.bashrc \
    && echo 'alias django-admin="pipenv run django-admin"' >> /home/app/.bashrc \
    && chmod +x /home/app/docker/entrypoint \
    && mkdir -p /home/app/data

WORKDIR /home/app/src

ENTRYPOINT ["/home/app/docker/entrypoint"]
