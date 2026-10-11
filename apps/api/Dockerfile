# Build from the repo root: docker build -f apps/api/Dockerfile .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_HTTP_TIMEOUT=120 UV_CONCURRENT_DOWNLOADS=8
WORKDIR /app

# Dependencies first (cached until a pyproject or the lockfile changes).
# The whole workspace is needed so uv can resolve uv.lock.
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
COPY packages/core/pyproject.toml packages/core/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package susanoo-api

COPY packages/core packages/core
COPY apps/api apps/api
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package susanoo-api

FROM python:3.12-slim
RUN useradd --system --uid 10001 --create-home susanoo
WORKDIR /app
COPY --from=build --chown=susanoo:susanoo /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER susanoo
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=4)"
CMD ["uvicorn", "susanoo_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
