# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app
# Dependencies first, from the lockfile and every workspace member's pyproject, so a code change
# does not reinstall Telethon. uv needs each member's pyproject to read the workspace at all,
# even while it installs none of them.
COPY pyproject.toml uv.lock README.md ./
COPY packages/ddiver-core/pyproject.toml ./packages/ddiver-core/
COPY packages/ddiver-core-testing/pyproject.toml ./packages/ddiver-core-testing/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace
# Then the code. --no-editable installs ddiver and ddiver-core into the venv as ordinary
# packages, so what runs does not depend on where the sources sit.
COPY ddiver ./ddiver
COPY packages ./packages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim

# Passed by deploy.sh so the running container can say which commit it is. Last, so a new
# value does not invalidate the layers above it.
ARG GIT_SHA=unknown

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/data \
    DDIVER_COMMIT=$GIT_SHA

RUN useradd --create-home --uid 10001 ddiver \
    && mkdir -p /data \
    && chown -R ddiver:ddiver /data

WORKDIR /app
COPY --from=builder --chown=ddiver:ddiver /app /app

USER ddiver
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["ddiver", "health"]

ENTRYPOINT ["ddiver"]
CMD ["run"]
