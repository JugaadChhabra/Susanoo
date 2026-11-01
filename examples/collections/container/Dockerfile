# Synthetic collections agent as an OCI image speaking JSONL protocol 1.0 on stdin/stdout (I08) [Spec §9, §19].
# Build context is assembled by prepare_context.py; run it only by digest through ContainerAgentAdapter.
FROM python:3.12-slim

ARG PYDANTIC_VERSION=2.14.0
ARG HTTPX_VERSION=0.28.1
ARG RFC8785_VERSION=0.1.4
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

RUN pip install --no-compile "pydantic==${PYDANTIC_VERSION}" "httpx==${HTTPX_VERSION}" "rfc8785==${RFC8785_VERSION}" tzdata

COPY app /app
WORKDIR /app
# Non-root; the adapter also forces --user, --read-only and --cap-drop ALL at run time.
USER 65532:65532
ENTRYPOINT ["python", "-m", "collections_demo.serve"]
CMD ["--mode", "correct"]
