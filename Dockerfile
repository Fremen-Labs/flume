FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/root/.local/bin:${PATH}" \
    UV_PROJECT_ENVIRONMENT=/opt/venv

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    build-essential \
    golang-go \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Pre-install common lint/check tools globally so agents can verify code
# in ephemeral worktrees without depending on project-local node_modules.
RUN npm install -g typescript@5.4.5 eslint@8.57.0 prettier@3.2.5 2>/dev/null || true

RUN curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /app

# Install deps into /opt/venv (outside /app) so the runtime .:/app volume mount
# does NOT shadow the installed packages. UV_PROJECT_ENVIRONMENT is set above
# and matched by docker-compose so both build and runtime use the same venv.
COPY pyproject.toml .
RUN uv venv /opt/venv && uv pip install --python /opt/venv --index-url "https://pypi.org/simple/?bust=1" -e .

COPY . /app

# Pre-compile the dashboard SPA (outDir: src/frontend/dist). Bind-mounting `.:/app` hides this
# layer on the host unless dist exists there; docker-compose runs a build-if-missing entrypoint too.
WORKDIR /app/src/frontend/src
RUN npm install && npm run build && rm -rf /app/src/frontend/src/node_modules
RUN cp -R /app/src/frontend/dist /dist-cache
WORKDIR /app

# Command is explicitly overridden per service via docker-compose.yml
CMD ["/opt/venv/bin/python", "-m", "src.dashboard.server"]
