FROM postgres:17-bookworm@sha256:839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5 AS pgtools
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PLATFORM_PG_BIN=/opt/postgres
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.11.19 \
    && groupadd --gid 10001 susanoo && useradd --uid 10001 --gid susanoo --create-home susanoo
WORKDIR /app
COPY --from=pgtools /usr/lib/postgresql/17/bin/pg_dump /usr/lib/postgresql/17/bin/pg_restore /opt/postgres/
COPY --from=pgtools /usr/lib/x86_64-linux-gnu/libpq.so.5 /usr/lib/x86_64-linux-gnu/libpq.so.5
RUN /opt/postgres/pg_dump --version && /opt/postgres/pg_restore --version
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY apps/api ./apps/api
COPY apps/worker ./apps/worker
COPY examples/collections/pyproject.toml ./examples/collections/pyproject.toml
COPY supabase/migrations ./supabase/migrations
RUN uv sync --frozen --no-dev --package susanoo-api && mkdir -m 0700 -p /app/.platform && chown -R susanoo:susanoo /app
USER susanoo
EXPOSE 8000
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "susanoo_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
