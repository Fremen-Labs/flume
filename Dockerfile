FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/root/.local/bin:${PATH}"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    build-essential \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /app

COPY pyproject.toml .
RUN uv pip install --system -e .

COPY . /app

# Pre-compile the dashboard SPA (outDir: src/frontend/dist). Bind-mounting `.:/app` hides this
# layer on the host unless dist exists there; docker-compose runs a build-if-missing entrypoint too.
WORKDIR /app/src/frontend/src
RUN npm ci && npm run build && rm -rf /app/src/frontend/src/node_modules
RUN cp -R /app/src/frontend/dist /dist-cache
WORKDIR /app

# Command is explicitly overridden per service via docker-compose.yml
CMD ["python", "-m", "src.dashboard.server"]
