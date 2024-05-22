#########################################################################################################
# Python compile image
#########################################################################################################
FROM python:3.11.7-slim-bullseye AS app-compiler

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  # dependencies for building Python packages
  build-essential \
  # mariadb dependencies
  pkg-config \
  libmariadb-dev

COPY Pipfile Pipfile.lock ./

RUN pip install pipenv \
  && pipenv requirements > /tmp/requirements.txt \
  && pip install --force-reinstall --compile --user --no-warn-script-location -r /tmp/requirements.txt


#########################################################################################################
# Python build image
#########################################################################################################
FROM python:3.11.7-slim-bullseye AS python-builder

LABEL maintainer="team@dipcode.com"

ARG VERSION
ARG BRANCH

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  # mariadb dependencies
  libmariadb3 \
  # File mimetype checker
  libmagic-dev \
  # Used on healthcheckers
  curl \
  # Translations
  gettext \
  # Add more media types
  media-types \
  # html to pdf generator (WeasyPrint)
  libpango-1.0-0 \
  libpangoft2-1.0-0 \
  # Cleaning up unused files
  && apt-get purge -y --auto-remove \
  -o APT::AutoRemove::RecommendsImportant=0 \
  -o APT::Autoremove::SuggestsImportant=0 \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* /var/cache/* \
  # Create aplication specific user
  && addgroup --system trueclinic \
  && adduser --system --ingroup trueclinic trueclinic

ENV PATH=/home/trueclinic/.local/bin:$PATH
ENV PYTHONPATH=$PYTHONPATH:/home/trueclinic/app/
ENV PYTHONUNBUFFERED=1
ENV VERSION=$VERSION
ENV BRANCH=$BRANCH

# Copy python compiled requirements
COPY --chown=trueclinic:trueclinic --from=app-compiler /root/.local /home/trueclinic/.local

# Copy application code
COPY --chown=trueclinic:trueclinic ./trueclinic /home/trueclinic/app
COPY --chown=trueclinic:trueclinic ./docker/production/app/entrypoint /home/trueclinic/entrypoint
COPY --chown=trueclinic:trueclinic ./docker/production/app/start-app /home/trueclinic/start-app
COPY --chown=trueclinic:trueclinic ./docker/production/app/start-worker /home/trueclinic/start-worker
COPY --chown=trueclinic:trueclinic ./docker/production/app/start-scheduler /home/trueclinic/start-scheduler

USER trueclinic

WORKDIR /home/trueclinic

RUN chmod +x entrypoint start-app start-worker start-scheduler \
  && python -m compileall app \
  && mkdir -p data \
  && django-admin compilemessages --ignore=.local/**/locale

ENTRYPOINT ["/home/trueclinic/entrypoint"]
CMD ["/home/trueclinic/start-app"]
