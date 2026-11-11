# Pinned by digest for reproducible builds; Dependabot proposes updates.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DBT_SEND_ANONYMOUS_USAGE_STATS=false

WORKDIR /app

RUN groupadd --gid 10001 election \
    && useradd --uid 10001 --gid 10001 --create-home election

# Dependencies first, from the lock alone: this layer is rebuilt only when the lock
# changes, so a code change reuses it and adds only the layers below.
COPY constraints.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=8 python -m pip install --no-deps -r constraints.txt

# Publication fingerprints expect source files underneath /app.
COPY pyproject.toml README.md requirements-dev.txt ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=8 python -m pip install --no-deps -e . \
    && python -m pip check

COPY config ./config
COPY dbt ./dbt
COPY deploy ./deploy
COPY migrations ./migrations
COPY scripts ./scripts
COPY tests ./tests

RUN mkdir -p /app/data \
    && chown -R 10001:10001 /app/data /app/dbt

USER 10001:10001

# Last: a build argument is an input to every later step, and the revision changes
# with each release; declared earlier it would rebuild the dependency layer every time.
ARG VCS_REF=unversioned
LABEL org.opencontainers.image.revision=$VCS_REF

CMD ["python", "-m", "election_pipeline.dashboard.server", "--host", "0.0.0.0"]
