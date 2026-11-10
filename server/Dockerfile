# syntax=docker/dockerfile:1
# One image for api, smtp, worker and migrate (different commands). Runs as an unprivileged user
# on a read-only filesystem (see deploy/docker-compose.yml).
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY alembic.ini ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin khugjil
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001
WORKDIR /app
EXPOSE 8000 2525
CMD ["uvicorn", "khugjil.web.app:production_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log", "--no-server-header"]
