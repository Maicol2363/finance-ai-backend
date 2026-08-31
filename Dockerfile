# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM python:3.14.7-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

COPY --from=uv /uv /uvx /bin/

RUN groupadd --system --gid 10001 finance-ai \
    && useradd --system --uid 10001 --gid finance-ai --home-dir /app finance-ai

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --frozen --no-dev \
    && chown -R finance-ai:finance-ai /app

USER finance-ai

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]

CMD ["uvicorn", "finance_ai.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test

USER root

COPY tests ./tests
COPY scripts ./scripts
RUN uv sync --frozen --all-groups \
    && chown -R finance-ai:finance-ai /app

USER finance-ai

CMD ["pytest"]
