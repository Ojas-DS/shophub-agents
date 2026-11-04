# syntax=docker/dockerfile:1
# One image, three services (store API, orders agent, orchestrator); the
# compose file picks the module to run. Multi-stage keeps build tools out of
# the runtime image; the app runs as a non-root user.

# ---------- build stage: resolve and install dependencies into a venv ----------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# ---------- runtime stage ----------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"
RUN groupadd --system app && useradd --system --gid app --home /app app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app scripts ./scripts
USER app
# PORT is set per service in docker-compose.yml
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=2)"
CMD ["python", "-m", "shophub.store_api"]
