FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && corepack enable \
 && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv

COPY . /app

RUN uv sync --frozen --no-dev
RUN cd /app/subsystems/china_channels_host && pnpm install --frozen-lockfile && pnpm run build

RUN mkdir -p /opt/g3ku-seed \
 && cp -R /app/skills /opt/g3ku-seed/skills \
 && cp -R /app/tools /opt/g3ku-seed/tools \
 && chmod +x /app/docker/web-entrypoint.sh /app/docker/worker-entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}" \
    G3KU_RESOURCE_SEED_ROOT=/opt/g3ku-seed

EXPOSE 18790
