FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY core/ core/
COPY gateway/ gateway/
COPY worker/ worker/
COPY tools/ tools/
COPY migrations/ migrations/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Entrypoints (gateway / worker) are defined at stage 2; single image, command per service.
CMD ["python", "-c", "import core, gateway, worker, tools; print('personal-assistant image ok')"]
