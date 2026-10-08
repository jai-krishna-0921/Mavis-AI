# syntax=docker/dockerfile:1.7
# Multi-arch (amd64 + arm64). The production box is a t4g.small (arm64) and builds this itself.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
# Every dependency ships an aarch64 wheel today (onnxruntime, pyahocorasick, psycopg-binary, ...);
# the toolchain is a safety net so a missing wheel compiles instead of failing the build.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY README.md alembic.ini ./
COPY src ./src
COPY scripts ./scripts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Bake the embedding model into the image so first boot does not stall on a download.
# Must match Settings.embedding_model; the app reads it from DATA_DIR/models.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
RUN /app/.venv/bin/python -c "from fastembed import TextEmbedding; TextEmbedding('${EMBEDDING_MODEL}', cache_dir='/app/data/models')"


FROM python:3.13-slim-bookworm AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 mavis
WORKDIR /app
COPY --from=builder --chown=mavis:mavis /app /app
RUN mkdir -p /app/data/artifacts && chown -R mavis:mavis /app/data
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data \
    ARTIFACTS_DIR=/app/data/artifacts
USER mavis
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["mavis", "api"]
