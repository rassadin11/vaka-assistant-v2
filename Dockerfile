FROM node:22-bookworm-slim AS miniapp-build

WORKDIR /miniapp

COPY miniapp/package.json miniapp/package-lock.json ./
RUN npm ci

COPY miniapp/ ./
RUN npm run build


FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY core/ core/
COPY gateway/ gateway/
COPY worker/ worker/
COPY webapp/ webapp/
COPY tools/ tools/
COPY migrations/ migrations/
RUN uv sync --frozen --no-dev

COPY --from=miniapp-build /miniapp/dist/ webapp/static/

ENV PATH="/app/.venv/bin:$PATH"

# tiktoken downloads its BPE table from an OpenAI CDN the first time an encoding
# is created, and core/tokens.py creates one at import. Warming the cache at
# build time means a container with no outbound internet still starts and still
# counts tokens (plan S14).
ENV TIKTOKEN_CACHE_DIR=/app/.tiktoken-cache
RUN mkdir -p "$TIKTOKEN_CACHE_DIR" \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && test -n "$(ls -A "$TIKTOKEN_CACHE_DIR")"

# Entrypoints (gateway / worker / webapp) are selected by their Compose service.
CMD ["python", "-c", "import core, gateway, worker, webapp, tools; print('personal-assistant image ok')"]
