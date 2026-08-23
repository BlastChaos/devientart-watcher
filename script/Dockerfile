# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies are installed before the source is copied, so an edit to the
# application does not invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM python:3.13-slim-bookworm AS runtime

# A fixed high UID keeps the image compatible with a restrictive
# PodSecurityContext and makes the PVC's ownership predictable.
RUN groupadd --gid 10001 dawatch \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin dawatch \
    && mkdir -p /data \
    && chown 10001:10001 /data

# WORKDIR runs as root and creates /app before the COPY below, so /app
# itself stays root-owned; only the copied .venv contents are chowned to
# the app user. Without this, buildkit's --chown on COPY would apply to
# the newly created /app parent directory too, making the whole
# application directory writable by the runtime user for no reason.
WORKDIR /app

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DAWATCH_DB_PATH=/data/dawatch.db \
    DAWATCH_ENV=prod

USER 10001:10001
VOLUME ["/data"]

ENTRYPOINT ["dawatch"]
CMD ["run"]
