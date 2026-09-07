FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

RUN python -m pip install --no-cache-dir uv

COPY . /app

RUN uv sync --frozen --no-dev

RUN mkdir -p /opt/g3ku-seed \
 && cp -R /app/skills /opt/g3ku-seed/skills \
 && cp -R /app/tools /opt/g3ku-seed/tools \
 && chmod +x /app/docker/web-entrypoint.sh /app/docker/worker-entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}" \
    G3KU_RESOURCE_SEED_ROOT=/opt/g3ku-seed

EXPOSE 18790
