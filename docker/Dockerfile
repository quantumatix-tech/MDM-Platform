FROM python:3.11-slim AS builder

WORKDIR /app

# pyproject.toml is the single source of truth for Python dependencies.
# The project source must be present for the build backend to resolve
# [project].dependencies, so the dependency layer is invalidated by source
# changes as well as by dependency changes.
COPY pyproject.toml ./
COPY core/ ./core/
COPY migration_platform/ ./migration_platform/
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    unixodbc \
    unixodbc-dev \
    msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

# Carries both the third-party dependencies and the project's own dist-info, so
# importlib.metadata.version("migration-platform") resolves at runtime.
COPY --from=builder /install /usr/local

WORKDIR /app

COPY config/ ./config/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]