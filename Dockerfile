# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY ddiver ./ddiver
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


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
