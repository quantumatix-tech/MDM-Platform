# Builder stage: install Python dependencies from the uv.lock lockfile.
#
# Reproducibility strategy:
#   * pyproject.toml remains the single source of truth for declared
#     dependencies. uv.lock pins every direct and transitive dependency to an
#     exact version with content hashes, so the same image is built from the
#     same set of packages on any machine with the same Python base image.
#   * The lockfile is generated with `uv lock` (uv 0.5.x) and consumed with
#     `uv export --format requirements-txt` to produce a hashed requirements
#     file. `uv pip install --require-hashes` then refuses to install any
#     package that is not pinned and hashed, which prevents silent upgrades or
#     downgrades of application dependencies at build time.
#   * The project itself is installed separately with --no-deps so its own
#     dist-info is present for importlib.metadata.version("migration-platform")
#     at runtime, while its dependencies are already satisfied by the locked
#     install above.
FROM python:3.11.9-slim AS builder

# Install uv 0.5.26 — the exact version used to generate uv.lock and verified
# for this project. The astral.sh installer script always fetches the latest
# release and does not accept a version argument, so the pinned release
# tarball is downloaded directly to guarantee the requested version is what
# actually runs during the build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && curl -fsSL \
        https://github.com/astral-sh/uv/releases/download/0.5.26/uv-x86_64-unknown-linux-gnu.tar.gz \
        -o /tmp/uv.tar.gz \
    && tar -xzf /tmp/uv.tar.gz -C /usr/local/bin --strip-components=1 \
    && rm /tmp/uv.tar.gz \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/local/bin:$PATH" \
    UV_LINK_MODE=copy

WORKDIR /app

# Copy the lockfile and project metadata first so the dependency layer is
# only rebuilt when dependencies change. The project source is copied too
# because the build backend needs it to resolve [project].dependencies and
# to build the wheel.
COPY pyproject.toml uv.lock ./
COPY core/ ./core/
COPY migration_platform/ ./migration_platform/

# Emit a hashed requirements file from the lockfile and install with hash
# verification enabled. --no-emit-package migration-platform keeps the project
# itself out of the requirements file (it is installed separately below).
RUN uv export --format requirements-txt \
        --no-dev \
        --no-editable \
        --no-header \
        --no-emit-package migration-platform \
        --output-file /tmp/requirements.locked.txt \
    && uv pip install --python /usr/local/bin/python \
        --require-hashes \
        --no-cache-dir \
        -r /tmp/requirements.locked.txt \
    && rm /tmp/requirements.locked.txt \
    && uv pip install --python /usr/local/bin/python \
        --no-deps \
        --no-cache-dir \
        .

# Runtime stage: minimal image with ODBC drivers for MSSQL connectivity.
# Same pinned base image as the builder stage.
FROM python:3.11.9-slim AS runtime

# Install MSODBC Driver 18 (required for MSSQL connectivity).
# The driver is not in the default Debian repos; add the Microsoft repository.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
    && curl -fsSL https://packages.microsoft.com/config/debian/11/packages-microsoft-prod.deb -o /tmp/packages-microsoft-prod.deb \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && rm /tmp/packages-microsoft-prod.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
        unixodbc \
        msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

# Carries both the third-party dependencies and the project's own dist-info, so
# importlib.metadata.version("migration-platform") resolves at runtime.
COPY --from=builder /usr/local /usr/local

WORKDIR /app

# NOTE: config/ is intentionally NOT copied into the image. Mount the host
# config/ directory (or a specific YAML file) at runtime so config files can
# be added, removed, or edited without rebuilding the image. Secrets must be
# supplied through environment variables or a secret manager at runtime, never
# baked into the image.
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV MIGRATION_PLATFORM_HOME=/app

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]