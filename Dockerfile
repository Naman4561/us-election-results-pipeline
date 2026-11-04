FROM python:3.12-slim-bookworm

ARG VCS_REF=unversioned
LABEL org.opencontainers.image.revision=$VCS_REF

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DBT_SEND_ANONYMOUS_USAGE_STATS=false

WORKDIR /app

RUN groupadd --gid 10001 election \
    && useradd --uid 10001 --gid 10001 --create-home election

COPY pyproject.toml README.md requirements-dev.txt ./
COPY src ./src

# Publication fingerprints expect source files underneath /app.
RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=8 python -m pip install \
    -e '.[postgres,transform,dashboard,deployment]' \
    -r requirements-dev.txt

COPY config ./config
COPY dbt ./dbt
COPY migrations ./migrations
COPY scripts ./scripts
COPY tests ./tests
COPY config ./config
COPY dbt ./dbt
COPY deploy ./deploy
COPY migrations ./migrations
COPY scripts ./scripts
COPY tests ./tests

RUN mkdir -p /app/data \
    && chown -R 10001:10001 /app/data /app/dbt

USER 10001:10001

CMD ["python", "-m", "election_pipeline.dashboard.server", "--host", "0.0.0.0"]
